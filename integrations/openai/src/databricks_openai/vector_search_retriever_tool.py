import logging
from typing import Dict, List, Optional, Tuple

from databricks.vector_search.client import VectorSearchIndex
from databricks_ai_bridge.utils.vector_search import (
    IndexDetails,
    parse_vector_search_response,
    validate_and_get_return_columns,
    validate_and_get_text_column,
)
from databricks_ai_bridge.vector_search_retriever_tool import (
    VectorSearchRetrieverToolInput,
    VectorSearchRetrieverToolMixin,
    vector_search_retriever_tool_trace,
)
from pydantic import Field, PrivateAttr, model_validator

from openai import OpenAI, pydantic_function_tool
from openai.types.chat import ChatCompletionToolParam

_logger = logging.getLogger(__name__)


class VectorSearchRetrieverTool(VectorSearchRetrieverToolMixin):
    """
    A utility class to create a vector search-based retrieval tool for querying indexed embeddings.
    This class integrates with Databricks Vector Search and provides a convenient interface
    for tool calling using the OpenAI SDK.

    Example:
        Step 1: Call model with VectorSearchRetrieverTool defined

        .. code-block:: python

            dbvs_tool = VectorSearchRetrieverTool(index_name="catalog.schema.my_index_name")
            messages = [
                {"role": "system", "content": "You are a helpful assistant."},
                {
                    "role": "user",
                    "content": "Using the Databricks documentation, answer what is Spark?",
                },
            ]
            first_response = client.chat.completions.create(
                model="gpt-4o", messages=messages, tools=[dbvs_tool.tool]
            )

        Step 2: Execute function code – parse the model's response and handle function calls.

        .. code-block:: python

            tool_call = first_response.choices[0].message.tool_calls[0]
            args = json.loads(tool_call.function.arguments)
            result = dbvs_tool.execute(
                query=args["query"]
            )  # For self-managed embeddings, optionally pass in openai_client=client

        Step 3: Supply model with results – so it can incorporate them into its final response.

        .. code-block:: python

            messages.append(first_response.choices[0].message)
            messages.append(
                {"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(result)}
            )
            second_response = client.chat.completions.create(
                model="gpt-4o", messages=messages, tools=tools
            )
    """

    text_column: Optional[str] = Field(
        None,
        description="The name of the text column to use for the embeddings. "
        "Required for direct-access index or delta-sync index with "
        "self-managed embeddings.",
    )
    embedding_model_name: Optional[str] = Field(
        None,
        description="The name of the embedding model to use for embedding the query text."
        "Required for direct-access index or delta-sync index with "
        "self-managed embeddings.",
    )

    tool: ChatCompletionToolParam = Field(
        None, description="The tool input used in the OpenAI chat completion SDK"
    )
    _index: VectorSearchIndex = PrivateAttr()
    _index_details: IndexDetails = PrivateAttr()

    @model_validator(mode="after")
    def _validate_tool_inputs(self):
        from databricks.vector_search.client import (
            VectorSearchClient,  # import here so we can mock in tests
        )

        splits = self.index_name.split(".")
        if len(splits) != 3:
            raise ValueError(
                f"Index name {self.index_name} is not in the expected format 'catalog.schema.index'."
            )
        self._index = VectorSearchClient(disable_notice=True).get_index(index_name=self.index_name)
        self._index_details = IndexDetails(self._index)
        self.text_column = validate_and_get_text_column(self.text_column, self._index_details)
        self.columns = validate_and_get_return_columns(
            self.columns or [], self.text_column, self._index_details
        )

        if (
            not self._index_details.is_databricks_managed_embeddings()
            and not self.embedding_model_name
        ):
            raise ValueError(
                "The embedding model name is required for non-Databricks-managed "
                "embeddings Vector Search indexes in order to generate embeddings for retrieval queries."
            )

        # OpenAI tool names must match the pattern '^[a-zA-Z0-9_-]+$'."
        # The '.' from the index name are not allowed
        def get_tool_name():
            tool_name = self.tool_name or self.index_name.replace(".", "__")
            if len(tool_name) > 64:
                _logger.warning(
                    f"Tool name {tool_name} is too long, truncating to 64 characters {tool_name[-64:]}."
                )
                return tool_name[-64:]
            return tool_name

        self.tool = pydantic_function_tool(
            VectorSearchRetrieverToolInput,
            name=get_tool_name(),
            description=self.tool_description
            or self._get_default_tool_description(self._index_details),
        )

        try:
            from databricks.sdk import WorkspaceClient
            from databricks.sdk.errors.platform import ResourceDoesNotExist

            WorkspaceClient().serving_endpoints.get(self.embedding_model_name)
            self.resources = self._get_resources(self.index_name, self.embedding_model_name)
        except ResourceDoesNotExist:
            self.resources = self._get_resources(self.index_name, None)

        return self

    @vector_search_retriever_tool_trace
    def execute(
        self,
        query: str,
        openai_client: OpenAI = None,
    ) -> List[Dict]:
        """
        Execute the VectorSearchIndex tool calls from the ChatCompletions response that correspond to the
        self.tool VectorSearchRetrieverToolInput and attach the retrieved documents into tool call messages.

        Args:
            query: The query text to use for the retrieval.
            openai_client: The OpenAI client object used to generate embeddings for retrieval queries. If not provided,
                           the default OpenAI client in the current environment will be used.

        Returns:
            A list of documents
        """

        if self._index_details.is_databricks_managed_embeddings():
            query_text, query_vector = query, None
        else:  # For non-Databricks-managed embeddings
            from openai import OpenAI

            oai_client = openai_client or OpenAI()
            if not oai_client.api_key:
                raise ValueError(
                    "OpenAI API key is required to generate embeddings for retrieval queries."
                )

            query_text = query if self.query_type and self.query_type.upper() == "HYBRID" else None
            query_vector = (
                oai_client.embeddings.create(input=query, model=self.embedding_model_name)
                .data[0]
                .embedding
            )
            if (
                index_embedding_dimension := self._index_details.embedding_vector_column.get(
                    "embedding_dimension"
                )
            ) and len(query_vector) != index_embedding_dimension:
                raise ValueError(
                    f"Expected embedding dimension {index_embedding_dimension} but got {len(query_vector)}"
                )

        search_resp = self._index.similarity_search(
            columns=self.columns,
            query_text=query_text,
            query_vector=query_vector,
            filters=self.filters,
            num_results=self.num_results,
            query_type=self.query_type,
        )
        docs_with_score: List[Tuple[Dict, float]] = parse_vector_search_response(
            search_resp=search_resp,
            index_details=self._index_details,
            text_column=self.text_column,
            document_class=dict,
        )
        return [doc for doc, _ in docs_with_score]

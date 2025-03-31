import json
import re
from typing import List, Optional, Union

import anthropic
from anthropic.types import Message as AnthropicMessage

from letta.errors import (
    ErrorCode,
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMConnectionError,
    LLMNotFoundError,
    LLMPermissionDeniedError,
    LLMRateLimitError,
    LLMServerError,
    LLMUnprocessableEntityError,
)
from letta.helpers.datetime_helpers import get_utc_time
from letta.llm_api.helpers import add_inner_thoughts_to_functions, unpack_all_inner_thoughts_from_kwargs
from letta.llm_api.llm_client_base import LLMClientBase
from letta.local_llm.constants import INNER_THOUGHTS_KWARG, INNER_THOUGHTS_KWARG_DESCRIPTION
from letta.log import get_logger
from letta.schemas.message import Message as PydanticMessage
from letta.schemas.openai.chat_completion_request import Tool
from letta.schemas.openai.chat_completion_response import ChatCompletionResponse, Choice, FunctionCall
from letta.schemas.openai.chat_completion_response import Message as ChoiceMessage
from letta.schemas.openai.chat_completion_response import ToolCall, UsageStatistics
from letta.services.provider_manager import ProviderManager

DUMMY_FIRST_USER_MESSAGE = "User initializing bootup sequence."

logger = get_logger(__name__)


class AnthropicClient(LLMClientBase):

    def request(self, request_data: dict) -> dict:
        client = self._get_anthropic_client(async_client=False)
        response = client.beta.messages.create(**request_data, betas=["tools-2024-04-04"])
        return response.model_dump()

    async def request_async(self, request_data: dict) -> dict:
        client = self._get_anthropic_client(async_client=True)
        response = await client.beta.messages.create(**request_data, betas=["tools-2024-04-04"])
        return response.model_dump()

    def _get_anthropic_client(self, async_client: bool = False) -> Union[anthropic.AsyncAnthropic, anthropic.Anthropic]:
        override_key = ProviderManager().get_anthropic_override_key()
        if async_client:
            return anthropic.AsyncAnthropic(api_key=override_key) if override_key else anthropic.AsyncAnthropic()
        return anthropic.Anthropic(api_key=override_key) if override_key else anthropic.Anthropic()

    def build_request_data(
        self,
        messages: List[PydanticMessage],
        tools: List[dict],
        tool_call: Optional[str],
        force_tool_call: Optional[str] = None,
    ) -> dict:
        prefix_fill = True
        if not self.use_tool_naming:
            raise NotImplementedError("Only tool calling supported on Anthropic API requests")

        if not self.llm_config.max_tokens:
            raise ValueError("Max  tokens must be set for anthropic")

        data = {
            "model": self.llm_config.model,
            "max_tokens": self.llm_config.max_tokens,
            "temperature": self.llm_config.temperature,
        }

        # Extended Thinking
        if self.llm_config.enable_reasoner:
            assert (
                self.llm_config.max_reasoning_tokens is not None and self.llm_config.max_reasoning_tokens < self.llm_config.max_tokens
            ), "max tokens must be greater than thinking budget"
            assert not self.llm_config.put_inner_thoughts_in_kwargs, "extended thinking not compatible with put_inner_thoughts_in_kwargs"

            data["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.llm_config.max_reasoning_tokens,
            }
            # `temperature` may only be set to 1 when thinking is enabled. Please consult our documentation at https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking#important-considerations-when-using-extended-thinking'
            data["temperature"] = 1.0

            # Silently disable prefix_fill for now
            prefix_fill = False

        # Tools
        tools_for_request = (
            [Tool(function=f) for f in tools if f["name"] == force_tool_call]
            if force_tool_call is not None
            else [Tool(function=f) for f in tools]
        )
        if force_tool_call is not None:
            self.llm_config.put_inner_thoughts_in_kwargs = True  # why do we do this ?

        # Add inner thoughts kwarg
        if len(tools_for_request) > 0 and self.llm_config.put_inner_thoughts_in_kwargs:
            tools_with_inner_thoughts = add_inner_thoughts_to_functions(
                functions=[t.function.model_dump() for t in tools_for_request],
                inner_thoughts_key=INNER_THOUGHTS_KWARG,
                inner_thoughts_description=INNER_THOUGHTS_KWARG_DESCRIPTION,
            )
            tools_for_request = [Tool(function=f) for f in tools_with_inner_thoughts]

        if len(tools_for_request) > 0:
            # TODO eventually enable parallel tool use
            data["tools"] = convert_tools_to_anthropic_format(tools_for_request)

        # Messages
        inner_thoughts_xml_tag = "thinking"
        data["messages"] = [
            m.to_anthropic_dict(
                inner_thoughts_xml_tag=inner_thoughts_xml_tag,
                put_inner_thoughts_in_kwargs=bool(self.llm_config.put_inner_thoughts_in_kwargs),
            )
            for m in messages
        ]

        # Move 'system' to the top level
        if data["messages"][0]["role"] != "system":
            raise RuntimeError(f"First message is not a system message, instead has role {data["messages"][0]["role"]}")

        data["system"] = data["messages"][0]["content"]
        data["messages"] = data["messages"][1:]

        # Ensure first message is user
        if data["messages"][0]["role"] != "user":
            data["messages"] = [{"role": "user", "content": DUMMY_FIRST_USER_MESSAGE}] + data["messages"]

        # Handle alternating messages
        data["messages"] = merge_tool_results_into_user_messages(data["messages"])

        # Prefix fill
        # https://docs.anthropic.com/en/api/messages#body-messages
        # NOTE: cannot prefill with tools for opus:
        # Your API request included an `assistant` message in the final position, which would pre-fill the `assistant` response. When using tools with "claude-3-opus-20240229"
        if prefix_fill and not self.llm_config.put_inner_thoughts_in_kwargs and "opus" not in data["model"]:
            data["messages"].append(
                # Start the thinking process for the assistant
                {"role": "assistant", "content": f"<{inner_thoughts_xml_tag}>"},
            )

        return data

    def handle_llm_error(self, e: Exception) -> Exception:
        if isinstance(e, anthropic.APIConnectionError):
            logger.warning(f"[Anthropic] API connection error: {e.__cause__}")
            return LLMConnectionError(
                message=f"Failed to connect to Anthropic: {str(e)}",
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                details={"cause": str(e.__cause__) if e.__cause__ else None},
            )

        if isinstance(e, anthropic.RateLimitError):
            logger.warning("[Anthropic] Rate limited (429). Consider backoff.")
            return LLMRateLimitError(
                message=f"Rate limited by Anthropic: {str(e)}",
                code=ErrorCode.RATE_LIMIT_EXCEEDED,
            )

        if isinstance(e, anthropic.BadRequestError):
            logger.warning(f"[Anthropic] Bad request: {str(e)}")
            return LLMBadRequestError(
                message=f"Bad request to Anthropic: {str(e)}",
                code=ErrorCode.INTERNAL_SERVER_ERROR,
            )

        if isinstance(e, anthropic.AuthenticationError):
            logger.warning(f"[Anthropic] Authentication error: {str(e)}")
            return LLMAuthenticationError(
                message=f"Authentication failed with Anthropic: {str(e)}",
                code=ErrorCode.INTERNAL_SERVER_ERROR,
            )

        if isinstance(e, anthropic.PermissionDeniedError):
            logger.warning(f"[Anthropic] Permission denied: {str(e)}")
            return LLMPermissionDeniedError(
                message=f"Permission denied by Anthropic: {str(e)}",
                code=ErrorCode.INTERNAL_SERVER_ERROR,
            )

        if isinstance(e, anthropic.NotFoundError):
            logger.warning(f"[Anthropic] Resource not found: {str(e)}")
            return LLMNotFoundError(
                message=f"Resource not found in Anthropic: {str(e)}",
                code=ErrorCode.INTERNAL_SERVER_ERROR,
            )

        if isinstance(e, anthropic.UnprocessableEntityError):
            logger.warning(f"[Anthropic] Unprocessable entity: {str(e)}")
            return LLMUnprocessableEntityError(
                message=f"Invalid request content for Anthropic: {str(e)}",
                code=ErrorCode.INTERNAL_SERVER_ERROR,
            )

        if isinstance(e, anthropic.APIStatusError):
            logger.warning(f"[Anthropic] API status error: {str(e)}")
            return LLMServerError(
                message=f"Anthropic API error: {str(e)}",
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                details={
                    "status_code": e.status_code if hasattr(e, "status_code") else None,
                    "response": str(e.response) if hasattr(e, "response") else None,
                },
            )

        return super().handle_llm_error(e)

    def convert_response_to_chat_completion(
        self,
        response_data: dict,
        input_messages: List[PydanticMessage],
    ) -> ChatCompletionResponse:
        """
        Example response from Claude 3:
        response.json = {
            'id': 'msg_01W1xg9hdRzbeN2CfZM7zD2w',
            'type': 'message',
            'role': 'assistant',
            'content': [
                {
                    'type': 'text',
                    'text': "<thinking>Analyzing user login event. This is Chad's first
        interaction with me. I will adjust my personality and rapport accordingly.</thinking>"
                },
                {
                    'type':
                    'tool_use',
                    'id': 'toolu_01Ka4AuCmfvxiidnBZuNfP1u',
                    'name': 'core_memory_append',
                    'input': {
                        'name': 'human',
                        'content': 'Chad is logging in for the first time. I will aim to build a warm
        and welcoming rapport.',
                        'request_heartbeat': True
                    }
                }
            ],
            'model': 'claude-3-haiku-20240307',
            'stop_reason': 'tool_use',
            'stop_sequence': None,
            'usage': {
                'input_tokens': 3305,
                'output_tokens': 141
            }
        }
        """
        response = AnthropicMessage(**response_data)
        prompt_tokens = response.usage.input_tokens
        completion_tokens = response.usage.output_tokens
        finish_reason = remap_finish_reason(str(response.stop_reason))

        content = None
        reasoning_content = None
        reasoning_content_signature = None
        redacted_reasoning_content = None
        tool_calls = None

        if len(response.content) > 0:
            for content_part in response.content:
                if content_part.type == "text":
                    content = strip_xml_tags(string=content_part.text, tag="thinking")
                if content_part.type == "tool_use":
                    tool_calls = [
                        ToolCall(
                            id=content_part.id,
                            type="function",
                            function=FunctionCall(
                                name=content_part.name,
                                arguments=json.dumps(content_part.input, indent=2),
                            ),
                        )
                    ]
                if content_part.type == "thinking":
                    reasoning_content = content_part.thinking
                    reasoning_content_signature = content_part.signature
                if content_part.type == "redacted_thinking":
                    redacted_reasoning_content = content_part.data

        else:
            raise RuntimeError("Unexpected empty content in response")

        assert response.role == "assistant"
        choice = Choice(
            index=0,
            finish_reason=finish_reason,
            message=ChoiceMessage(
                role=response.role,
                content=content,
                reasoning_content=reasoning_content,
                reasoning_content_signature=reasoning_content_signature,
                redacted_reasoning_content=redacted_reasoning_content,
                tool_calls=tool_calls,
            ),
        )

        chat_completion_response = ChatCompletionResponse(
            id=response.id,
            choices=[choice],
            created=get_utc_time(),
            model=response.model,
            usage=UsageStatistics(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )
        if self.llm_config.put_inner_thoughts_in_kwargs:
            chat_completion_response = unpack_all_inner_thoughts_from_kwargs(
                response=chat_completion_response, inner_thoughts_key=INNER_THOUGHTS_KWARG
            )

        return chat_completion_response


def convert_tools_to_anthropic_format(tools: List[Tool]) -> List[dict]:
    """See: https://docs.anthropic.com/claude/docs/tool-use

    OpenAI style:
      "tools": [{
        "type": "function",
        "function": {
            "name": "find_movies",
            "description": "find ....",
            "parameters": {
              "type": "object",
              "properties": {
                 PARAM: {
                   "type": PARAM_TYPE,  # eg "string"
                   "description": PARAM_DESCRIPTION,
                 },
                 ...
              },
              "required": List[str],
            }
        }
      }
      ]

    Anthropic style:
      "tools": [{
        "name": "find_movies",
        "description": "find ....",
        "input_schema": {
          "type": "object",
          "properties": {
             PARAM: {
               "type": PARAM_TYPE,  # eg "string"
               "description": PARAM_DESCRIPTION,
             },
             ...
          },
          "required": List[str],
        }
      }
      ]

      Two small differences:
        - 1 level less of nesting
        - "parameters" -> "input_schema"
    """
    formatted_tools = []
    for tool in tools:
        formatted_tool = {
            "name": tool.function.name,
            "description": tool.function.description,
            "input_schema": tool.function.parameters or {"type": "object", "properties": {}, "required": []},
        }
        formatted_tools.append(formatted_tool)

    return formatted_tools


def merge_tool_results_into_user_messages(messages: List[dict]):
    """Anthropic API doesn't allow role 'tool'->'user' sequences

    Example HTTP error:
    messages: roles must alternate between "user" and "assistant", but found multiple "user" roles in a row

    From: https://docs.anthropic.com/claude/docs/tool-use
    You may be familiar with other APIs that return tool use as separate from the model's primary output,
    or which use a special-purpose tool or function message role.
    In contrast, Anthropic's models and API are built around alternating user and assistant messages,
    where each message is an array of rich content blocks: text, image, tool_use, and tool_result.
    """

    # TODO walk through the messages list
    # When a dict (dict_A) with 'role' == 'user' is followed by a dict with 'role' == 'user' (dict B), do the following
    # dict_A["content"] = dict_A["content"] + dict_B["content"]

    # The result should be a new merged_messages list that doesn't have any back-to-back dicts with 'role' == 'user'
    merged_messages = []
    if not messages:
        return merged_messages

    # Start with the first message in the list
    current_message = messages[0]

    for next_message in messages[1:]:
        if current_message["role"] == "user" and next_message["role"] == "user":
            # Merge contents of the next user message into current one
            current_content = (
                current_message["content"]
                if isinstance(current_message["content"], list)
                else [{"type": "text", "text": current_message["content"]}]
            )
            next_content = (
                next_message["content"]
                if isinstance(next_message["content"], list)
                else [{"type": "text", "text": next_message["content"]}]
            )
            merged_content = current_content + next_content
            current_message["content"] = merged_content
        else:
            # Append the current message to result as it's complete
            merged_messages.append(current_message)
            # Move on to the next message
            current_message = next_message

    # Append the last processed message to the result
    merged_messages.append(current_message)

    return merged_messages


def remap_finish_reason(stop_reason: str) -> str:
    """Remap Anthropic's 'stop_reason' to OpenAI 'finish_reason'

    OpenAI: 'stop', 'length', 'function_call', 'content_filter', null
    see: https://platform.openai.com/docs/guides/text-generation/chat-completions-api

    From: https://docs.anthropic.com/claude/reference/migrating-from-text-completions-to-messages#stop-reason

    Messages have a stop_reason of one of the following values:
        "end_turn": The conversational turn ended naturally.
        "stop_sequence": One of your specified custom stop sequences was generated.
        "max_tokens": (unchanged)

    """
    if stop_reason == "end_turn":
        return "stop"
    elif stop_reason == "stop_sequence":
        return "stop"
    elif stop_reason == "max_tokens":
        return "length"
    elif stop_reason == "tool_use":
        return "function_call"
    else:
        raise ValueError(f"Unexpected stop_reason: {stop_reason}")


def strip_xml_tags(string: str, tag: Optional[str]) -> str:
    if tag is None:
        return string
    # Construct the regular expression pattern to find the start and end tags
    tag_pattern = f"<{tag}.*?>|</{tag}>"
    # Use the regular expression to replace the tags with an empty string
    return re.sub(tag_pattern, "", string)


def strip_xml_tags_streaming(string: str, tag: Optional[str]) -> str:
    if tag is None:
        return string

    # Handle common partial tag cases
    parts_to_remove = [
        "<",  # Leftover start bracket
        f"<{tag}",  # Opening tag start
        f"</{tag}",  # Closing tag start
        f"/{tag}>",  # Closing tag end
        f"{tag}>",  # Opening tag end
        f"/{tag}",  # Partial closing tag without >
        ">",  # Leftover end bracket
    ]

    result = string
    for part in parts_to_remove:
        result = result.replace(part, "")

    return result

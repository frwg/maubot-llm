from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout


class BackendError(Exception):
    """An error that can be safely reported to a user."""


class ChatCompletion:
    def __init__(
        self, message: dict[str, Any], finish_reason: str | None, model: str | None
    ) -> None:
        self.message = message
        self.finish_reason = finish_reason
        self.model = model

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, ChatCompletion)
            and self.message == other.message
            and self.finish_reason == other.finish_reason
            and self.model == other.model
        )


class Backend:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.default_model = cfg.get("default_model")
        self.default_system_prompt = cfg.get("default_system_prompt")
        timeout = float(cfg.get("timeout", 120))
        if not 1 <= timeout <= 600:
            raise ValueError("Backend timeout must be between 1 and 600 seconds")
        self.timeout = ClientTimeout(total=timeout)
        self.base_url = self._validate_base_url(cfg.get("base_url", ""))

    @staticmethod
    def _validate_base_url(value: str) -> str:
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as err:
            raise ValueError("Backend base_url has an invalid port") from err
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not parsed.hostname
            or (port is not None and not 1 <= port <= 65535)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Backend base_url must be an absolute HTTP(S) URL without credentials"
            )
        return value.rstrip("/")

    async def create_chat_completion(
        self,
        http: ClientSession,
        context: list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
    ) -> ChatCompletion:
        raise NotImplementedError

    async def fetch_models(self, http: ClientSession) -> list[str]:
        raise NotImplementedError

    async def _request_json(
        self, http: ClientSession, method: str, url: str, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            async with http.request(
                method,
                url,
                timeout=self.timeout,
                allow_redirects=False,
                **kwargs,
            ) as response:
                if response.status >= 400:
                    if response.status == 401 or response.status == 403:
                        reason = "The LLM service rejected authentication."
                    elif response.status == 404:
                        reason = "The LLM endpoint or model was not found."
                    elif response.status == 429:
                        reason = "The LLM service is rate limiting requests. Please try again later."
                    elif response.status >= 500:
                        reason = "The LLM service is temporarily unavailable."
                    else:
                        reason = f"The LLM service returned HTTP {response.status}."
                    raise BackendError(reason)
                try:
                    data = await response.json()
                except (ValueError, ClientError) as err:
                    raise BackendError(
                        "The LLM service returned invalid JSON."
                    ) from err
        except BackendError:
            raise
        except TimeoutError as err:
            raise BackendError("The LLM request timed out.") from err
        except ClientError as err:
            raise BackendError("Could not connect to the LLM service.") from err
        if not isinstance(data, dict):
            raise BackendError("The LLM service returned an invalid response.")
        return data


class BasicOpenAIBackend(Backend):
    def __init__(self, cfg: dict[str, Any]) -> None:
        cfg = {**cfg, "base_url": cfg.get("base_url", "")}
        super().__init__(cfg)
        authorization = cfg.get("authorization")
        api_key = cfg.get("api_key")
        self.headers: dict[str, str] = {}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        elif authorization:
            self.headers["Authorization"] = authorization

    async def create_chat_completion(
        self,
        http: ClientSession,
        context: list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
    ) -> ChatCompletion:
        messages = list(context)
        if system:
            messages.insert(0, {"role": "system", "content": system})
        request: dict[str, Any] = {"messages": messages}
        if model:
            request["model"] = model
        for key in (
            "temperature",
            "top_p",
            "max_tokens",
            "presence_penalty",
            "frequency_penalty",
        ):
            if self.cfg.get(key) is not None:
                request[key] = self.cfg[key]
        data = await self._request_json(
            http,
            "POST",
            f"{self.base_url}/v1/chat/completions",
            headers=self.headers,
            json=request,
        )
        try:
            choice = data["choices"][0]
            message = choice["message"]
            if (
                not isinstance(message, dict)
                or message.get("role") != "assistant"
                or not isinstance(message.get("content"), str)
                or not message["content"].strip()
            ):
                raise ValueError
            return ChatCompletion(
                message=message,
                finish_reason=choice.get("finish_reason"),
                model=data.get("model"),
            )
        except (KeyError, IndexError, TypeError, ValueError) as err:
            raise BackendError(
                "The LLM service returned no usable assistant message."
            ) from err

    async def fetch_models(self, http: ClientSession) -> list[str]:
        data = await self._request_json(
            http, "GET", f"{self.base_url}/v1/models", headers=self.headers
        )
        try:
            models = [item["id"] for item in data["data"]]
            if not all(isinstance(model, str) for model in models):
                raise ValueError
            return models
        except (KeyError, TypeError, ValueError) as err:
            raise BackendError(
                "The LLM service returned an invalid model list."
            ) from err


class OpenAIBackend(BasicOpenAIBackend):
    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(
            {**cfg, "base_url": cfg.get("base_url", "https://api.openai.com")}
        )


class AnthropicBackend(Backend):
    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(
            {**cfg, "base_url": cfg.get("base_url", "https://api.anthropic.com")}
        )
        self.api_key = cfg.get("api_key", "")
        self.max_tokens = int(cfg.get("max_tokens", 1024))

    async def create_chat_completion(
        self,
        http: ClientSession,
        context: list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
    ) -> ChatCompletion:
        messages = [
            dict(message) for message in context if message.get("role") != "system"
        ]
        request: dict[str, Any] = {"messages": messages, "max_tokens": self.max_tokens}
        if system:
            request["system"] = system
        if model:
            request["model"] = model
        data = await self._request_json(
            http,
            "POST",
            f"{self.base_url}/v1/messages",
            headers={
                "anthropic-version": "2023-06-01",
                "x-api-key": self.api_key,
            },
            json=request,
        )
        try:
            text = "\n\n".join(
                block["text"] for block in data["content"] if block["type"] == "text"
            )
            if not text.strip():
                raise ValueError
            return ChatCompletion(
                message={"role": "assistant", "content": text},
                finish_reason=data["stop_reason"],
                model=data["model"],
            )
        except (KeyError, TypeError, ValueError) as err:
            raise BackendError(
                "The LLM service returned no usable assistant message."
            ) from err

    async def fetch_models(self, http: ClientSession) -> list[str]:
        raise BackendError(
            "Model discovery is not supported by the configured Anthropic API."
        )

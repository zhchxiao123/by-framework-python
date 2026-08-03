"""The chat-ui HTTP + WebSocket server.

This is the primary test seam for the whole package: tests drive the real
`aiohttp.web.Application` returned by `create_app` over its real HTTP (and,
from slice 2 on, WebSocket) boundary, with only Redis and Postgres faked at
their own boundaries underneath.
"""

from __future__ import annotations

import argparse
import json
from importlib.resources import files

from aiohttp import WSMsgType, web
from by_framework.client.byai_client import ByaiGatewayClient
from by_framework.client.client import GatewayClient
from by_framework.common.config import RedisConfig
from by_framework.common.redis_client import init_redis
from by_framework.core.registry import WorkerRegistry

from .agents import list_online_agent_types
from .conversations import WAITING_USER, Conversation, ConversationStore
from .gateway import (
    AgentUnavailableError,
    TurnTimeoutError,
    dispatch_and_await,
    stream_turn,
)
from .hub import WebSocketHub
from .protocol import AnswerChunk, AskUser, FinalAnswer, StreamEnd
from .storage import ChatHistoryStore

AGENT_UNAVAILABLE_MESSAGE = "该助手当前不可用"
CONVERSATION_LOCKED_MESSAGE = "请等待当前回复完成"
STATIC_PACKAGE = "by_framework_chat_ui.static"
TITLE_MAX_LENGTH = 20
STATIC_CONTENT_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".svg": "image/svg+xml",
}

GATEWAY_CLIENT_KEY: web.AppKey[GatewayClient] = web.AppKey(
    "gateway_client", GatewayClient
)
REGISTRY_KEY: web.AppKey[WorkerRegistry] = web.AppKey("registry", WorkerRegistry)
CONVERSATIONS_KEY: web.AppKey[ConversationStore] = web.AppKey(
    "conversations", ConversationStore
)
HISTORY_STORE_KEY: web.AppKey[ChatHistoryStore] = web.AppKey(
    "history_store", ChatHistoryStore
)
HUB_KEY: web.AppKey[WebSocketHub] = web.AppKey("hub", WebSocketHub)


def create_app(
    *,
    gateway_client: GatewayClient,
    registry: WorkerRegistry,
    history_store: ChatHistoryStore,
    conversations: ConversationStore | None = None,
    hub: WebSocketHub | None = None,
) -> web.Application:
    """Build the chat-ui aiohttp application.

    `history_store` is required, not optional: persisting Conversations is
    this product's whole point (a service restart must not lose chat
    history), so there is no silently-degraded no-persistence mode.
    """
    app = web.Application()
    app[GATEWAY_CLIENT_KEY] = gateway_client
    app[REGISTRY_KEY] = registry
    app[CONVERSATIONS_KEY] = conversations or ConversationStore()
    app[HISTORY_STORE_KEY] = history_store
    app[HUB_KEY] = hub or WebSocketHub()

    app.router.add_get("/api/agents", _list_agents)
    app.router.add_post("/api/conversations", _create_conversation)
    app.router.add_get("/api/conversations", _list_conversations)
    app.router.add_get("/api/conversations/{session_id}", _get_conversation)
    app.router.add_post("/api/conversations/{session_id}/messages", _send_message)
    app.router.add_get("/ws/conversations/{session_id}", _ws_conversation)
    app.router.add_get("/", _index)
    app.router.add_get("/{asset_name}", _static_asset)
    return app


def read_static_asset(asset_name: str) -> tuple[bytes, str]:
    """Read a packaged static asset (the Vite-built frontend) by name."""
    normalized = asset_name.strip("/") or "index.html"
    if "/" in normalized or normalized.startswith("."):
        raise FileNotFoundError(normalized)

    resource = files(STATIC_PACKAGE).joinpath(normalized)
    if not resource.is_file():
        raise FileNotFoundError(normalized)

    suffix = "." + normalized.rsplit(".", 1)[-1] if "." in normalized else ""
    content_type = STATIC_CONTENT_TYPES.get(suffix, "application/octet-stream")
    return resource.read_bytes(), content_type


async def _resolve_conversation(request: web.Request, session_id: str):
    """Look up a Conversation in memory, falling back to Postgres.

    A Conversation created before this process last restarted won't be in
    the in-memory `ConversationStore` — rehydrate it from the Chat History
    Store so turn-state tracking still works for it going forward.
    """
    conversations = request.app[CONVERSATIONS_KEY]
    conversation = conversations.get(session_id)
    if conversation is not None:
        return conversation

    record = await request.app[HISTORY_STORE_KEY].get_conversation(session_id)
    if record is None:
        return None
    return conversations.rehydrate(
        session_id,
        record["agent_type"],
        turn_state=record["turn_state"],
        last_message_id=record["last_message_id"],
    )


async def _resolve_message_id(
    conversation: Conversation, history_store: ChatHistoryStore
) -> tuple[str, bool]:
    """Return `(message_id, is_resume)` for the turn about to be dispatched.

    `GatewayClient.send_message` reuses a RESUME's `message_id` to look the
    suspended execution back up (`get_execution_by_message_id`); passing a
    freshly-generated one — which is what happens if the caller never
    threads one through explicitly — silently mints a new orphaned
    execution instead of resuming the suspended one. A new `message_id` is
    only minted (and persisted, so a later restart-then-RESUME can still
    find it via `_resolve_conversation`'s rehydrate) when this isn't a
    resume of a still-open `ask_user` wait.
    """
    if conversation.turn_state == WAITING_USER and conversation.last_message_id:
        return conversation.last_message_id, True

    message_id = conversation.generate_message_id()
    conversation.last_message_id = message_id
    await history_store.set_last_message_id(conversation.session_id, message_id)
    return message_id, False


async def _index(request: web.Request) -> web.Response:
    body, content_type = read_static_asset("index.html")
    return web.Response(body=body, content_type=content_type)


async def _static_asset(request: web.Request) -> web.Response:
    asset_name = request.match_info["asset_name"]
    try:
        body, content_type = read_static_asset(asset_name)
    except FileNotFoundError:
        return web.json_response({"error": "not found"}, status=404)
    return web.Response(body=body, content_type=content_type)


async def _list_agents(request: web.Request) -> web.Response:
    agent_types = await list_online_agent_types(request.app[REGISTRY_KEY])
    return web.json_response({"agent_types": agent_types})


async def _create_conversation(request: web.Request) -> web.Response:
    body = await request.json()
    agent_type = str(body.get("agent_type") or "").strip()
    if not agent_type:
        return web.json_response({"error": "agent_type is required"}, status=400)

    conversation = request.app[CONVERSATIONS_KEY].create(agent_type)
    await request.app[HISTORY_STORE_KEY].create_conversation(
        conversation.session_id, conversation.agent_type
    )
    return web.json_response(
        {"session_id": conversation.session_id, "agent_type": conversation.agent_type}
    )


async def _list_conversations(request: web.Request) -> web.Response:
    conversations = await request.app[HISTORY_STORE_KEY].list_conversations()
    return web.json_response({"conversations": conversations})


async def _get_conversation(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    conversation = await _resolve_conversation(request, session_id)
    if conversation is None:
        return web.json_response({"error": "conversation not found"}, status=404)

    messages = await request.app[HISTORY_STORE_KEY].get_messages(session_id)
    return web.json_response(
        {
            "session_id": conversation.session_id,
            "agent_type": conversation.agent_type,
            "messages": messages,
        }
    )


async def _send_message(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    conversation = await _resolve_conversation(request, session_id)
    if conversation is None:
        return web.json_response({"error": "conversation not found"}, status=404)

    body = await request.json()
    content = str(body.get("content") or "")
    if not content:
        return web.json_response({"error": "content is required"}, status=400)

    if not conversation.try_lock():
        return web.json_response({"error": CONVERSATION_LOCKED_MESSAGE}, status=423)

    hub = request.app[HUB_KEY]
    await hub.broadcast(session_id, {"type": "locked"})

    history_store = request.app[HISTORY_STORE_KEY]
    try:
        await history_store.save_message(session_id, "user", content)
        await history_store.set_initial_title(session_id, content[:TITLE_MAX_LENGTH])

        message_id, is_resume = await _resolve_message_id(conversation, history_store)
        try:
            result = await dispatch_and_await(
                request.app[GATEWAY_CLIENT_KEY],
                session_id=session_id,
                agent_type=conversation.agent_type,
                content=content,
                action_type=conversation.next_action_type(),
                message_id=message_id,
                parent_message_id=message_id if is_resume else "",
            )
        except AgentUnavailableError:
            return web.json_response({"error": AGENT_UNAVAILABLE_MESSAGE}, status=409)

        conversation.apply_turn_result(result.status)
        await history_store.set_turn_state(session_id, conversation.turn_state)
        await history_store.save_message(
            session_id,
            "assistant",
            result.content,
            is_ask_user=(result.status == "waiting_user"),
        )
        return web.json_response(
            {"role": "assistant", "status": result.status, "content": result.content}
        )
    finally:
        conversation.unlock()
        await hub.broadcast(session_id, {"type": "unlocked"})


def _ws_event_payload(event) -> dict:
    if isinstance(event, AnswerChunk):
        return {"type": "chunk", "content": event.content}
    if isinstance(event, FinalAnswer):
        return {"type": "final", "content": event.content}
    if isinstance(event, AskUser):
        return {"type": "ask_user", "prompt": event.prompt}
    if isinstance(event, StreamEnd):
        return {"type": "turn_complete"}
    return {"type": "other"}


async def _ws_conversation(request: web.Request) -> web.WebSocketResponse:
    session_id = request.match_info["session_id"]
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    conversation = await _resolve_conversation(request, session_id)
    if conversation is None:
        await ws.send_json({"type": "error", "message": "conversation not found"})
        await ws.close()
        return ws

    hub = request.app[HUB_KEY]
    hub.subscribe(session_id, ws)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "invalid message"})
                continue

            content = str(payload.get("content") or "")
            if not content:
                await ws.send_json({"type": "error", "message": "content is required"})
                continue

            if not conversation.try_lock():
                await ws.send_json(
                    {"type": "error", "message": CONVERSATION_LOCKED_MESSAGE}
                )
                continue

            await hub.broadcast(session_id, {"type": "locked"})
            history_store = request.app[HISTORY_STORE_KEY]
            try:
                await history_store.save_message(session_id, "user", content)
                await history_store.set_initial_title(
                    session_id, content[:TITLE_MAX_LENGTH]
                )

                message_id, is_resume = await _resolve_message_id(
                    conversation, history_store
                )
                try:
                    turn_status = "completed"
                    accumulated_text = ""
                    ask_user_prompt = ""
                    async for event in stream_turn(
                        request.app[GATEWAY_CLIENT_KEY],
                        session_id=session_id,
                        agent_type=conversation.agent_type,
                        content=content,
                        action_type=conversation.next_action_type(),
                        message_id=message_id,
                        parent_message_id=message_id if is_resume else "",
                    ):
                        if isinstance(event, AnswerChunk):
                            accumulated_text += event.content
                        elif isinstance(event, FinalAnswer):
                            accumulated_text = event.content
                        elif isinstance(event, AskUser):
                            turn_status = "waiting_user"
                            ask_user_prompt = event.prompt
                        elif isinstance(event, StreamEnd):
                            turn_status = "completed"
                        try:
                            await ws.send_json(_ws_event_payload(event))
                        except ConnectionResetError:
                            # The client is gone; stop trying to push further
                            # events for this turn but keep the connection
                            # object intact.
                            break
                    conversation.apply_turn_result(turn_status)
                    await history_store.set_turn_state(
                        session_id, conversation.turn_state
                    )
                    is_waiting_user = turn_status == "waiting_user"
                    await history_store.save_message(
                        session_id,
                        "assistant",
                        ask_user_prompt if is_waiting_user else accumulated_text,
                        is_ask_user=is_waiting_user,
                    )
                except AgentUnavailableError:
                    await ws.send_json(
                        {"type": "error", "message": AGENT_UNAVAILABLE_MESSAGE}
                    )
                except TurnTimeoutError as exc:
                    await ws.send_json({"type": "error", "message": str(exc)})
            finally:
                conversation.unlock()
                await hub.broadcast(session_id, {"type": "unlocked"})
    finally:
        hub.unsubscribe(session_id, ws)

    return ws


def parse_args() -> argparse.Namespace:
    """Parse chat-ui CLI arguments."""
    config = RedisConfig.from_env()
    parser = argparse.ArgumentParser(description="Serve the by-framework chat UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--redis-host", default=config.host)
    parser.add_argument("--redis-port", type=int, default=config.port)
    parser.add_argument("--redis-db", type=int, default=config.db)
    parser.add_argument("--redis-username", default=config.username)
    parser.add_argument("--redis-password", default=config.password)
    parser.add_argument(
        "--redis-max-connections", type=int, default=config.max_connections
    )
    return parser.parse_args()


def main() -> None:
    """CLI entry point for the chat-ui server."""
    args = parse_args()
    redis = init_redis(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
        username=args.redis_username,
        password=args.redis_password,
        max_connections=args.redis_max_connections,
    )
    registry = WorkerRegistry(redis)
    gateway_client = ByaiGatewayClient(redis_client=redis, registry=registry)
    history_store = ChatHistoryStore()
    app = create_app(
        gateway_client=gateway_client, registry=registry, history_store=history_store
    )
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

"""Trusted built-in request-handler factories and their shared startup services.

This is application composition, not a third-party discovery or execution API.
New handlers join this factory table; the application registers every result.
Runtime services are still bound by their existing owners after construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from server.handlers.voice import VoiceHandlers
from server.ws_handler import ConnectionManager, RequestHandler


@dataclass(frozen=True)
class HandlerServices:
    capability_catalog: Any
    work_ledger: Any
    work_ledger_store: Any
    work_preview: Any
    attention: Any
    auip_launch: Any
    provider_activity: Any
    provider_runtime: Any
    current_session_id: Callable[[], str]
    auip_app_websocket_url: str
    extra_capability_packages: Callable[[], Any]


@dataclass
class BuiltinHandlers:
    instances: dict[str, RequestHandler]
    voice: VoiceHandlers

    def register(self, manager: ConnectionManager) -> None:
        for handler in self.instances.values():
            manager.register_handler(handler)


def builtin_handler_factories(
    services: HandlerServices, voice: VoiceHandlers,
) -> dict[str, Callable[[], RequestHandler]]:
    from server.chat_role_delivery import ChatRoleDelivery
    from server.handlers.attention_handler import AttentionRequestHandler
    from server.handlers.auip_handler import AuipHandler
    from server.handlers.capability_handler import CapabilityHandler
    from server.handlers.character_handler import CharacterHandler
    from server.handlers.chat_handler import ChatHandler
    from server.handlers.expression_handler import ExpressionHandler
    from server.handlers.mcp_connection_handler import McpConnectionHandler
    from server.handlers.provider_activity_handler import ProviderActivityHandler
    from server.handlers.provider_handler import ProviderHandler
    from server.handlers.render_handler import RenderHandler
    from server.handlers.session_handler import SessionHandler
    from server.handlers.system_handler import SystemHandler
    from server.handlers.visual_handler import VisualHandler
    from server.handlers.vn_launch_handler import VNLaunchHandler
    from server.handlers.vn_player_handler import VNPlayerHandler
    from server.handlers.vts_handler import VtsHandler
    from server.handlers.wallpaper_handler import WallpaperHandler
    from server.handlers.work_ledger_handler import WorkLedgerHandler
    from server.work_preview import WorkPreviewHandler

    # These shared adapters are dependencies of sibling handlers. Construct
    # each once and bind explicitly; there is no implicit dependency resolver.
    provider = ProviderHandler(capability_catalog=services.capability_catalog)
    preview = WorkPreviewHandler(services.work_ledger, services.work_preview)
    return {
        "chat": ChatHandler,
        "session": SessionHandler,
        "character": CharacterHandler,
        "tts": lambda: voice.tts,
        "asr": lambda: voice.asr,
        "wake": lambda: voice.wake,
        "vts": VtsHandler,
        "expression": ExpressionHandler,
        "system": SystemHandler,
        "render": RenderHandler,
        "wallpaper": WallpaperHandler,
        "provider": lambda: provider,
        "capability": lambda: CapabilityHandler(
            services.capability_catalog, extra_packages=services.extra_capability_packages,
        ),
        "mcp_connection": lambda: McpConnectionHandler(provider.mcp_connections),
        "provider_activity": lambda: ProviderActivityHandler(services.provider_activity),
        "work": lambda: WorkLedgerHandler(
            services.work_ledger,
            provider_run=provider.run_provider,
            provider_permission=services.provider_runtime.resolve_permission,
            provider_input=services.provider_runtime.append_input,
            preview_open=preview.open_from_work_action,
        ),
        "work_preview": lambda: preview,
        "attention": lambda: AttentionRequestHandler(
            services.attention, current_session_id=services.current_session_id,
        ),
        "auip": lambda: AuipHandler(
            artifacts=services.work_ledger_store,
            current_session_id=services.current_session_id,
            app_websocket_url=services.auip_app_websocket_url,
            launch=services.auip_launch,
            preview_handoff=services.work_preview.begin_auip_handoff,
        ),
        "vn": VNPlayerHandler,
        "vn_launch": VNLaunchHandler,
        "visual": VisualHandler,
        "chat_role_delivery": ChatRoleDelivery,
    }


def create_builtin_handlers(services: HandlerServices) -> BuiltinHandlers:
    voice = VoiceHandlers()
    handlers = {
        name: factory() for name, factory in builtin_handler_factories(services, voice).items()
    }
    handlers["session"].configure(
        work_coordinator=services.work_ledger, is_chat_busy=handlers["chat"].is_busy,
    )
    handlers["provider"].configure_work_control(services.work_ledger)
    services.auip_launch.before_result_entry = handlers["auip"].prepare_result_entry
    return BuiltinHandlers(handlers, voice)

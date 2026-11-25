"""Explicit desktop memory operations; conversational text never invokes writes."""
import logging

from server.memory_service import MemoryRequestError
from server.protocol import Method
from server.ws_handler import RequestHandler

logger = logging.getLogger(__name__)


class MemoryHandler(RequestHandler):
    methods = [Method.MEMORY_STATUS, Method.MEMORY_LIST, Method.MEMORY_GET,
        Method.MEMORY_REMEMBER, Method.MEMORY_CORRECT, Method.MEMORY_FORGET, Method.MEMORY_SOURCES]
    methods.append(Method.MEMORY_FORGET_PREVIEW)

    def __init__(self, service):
        self.service = service

    async def handle(self, method, params):
        if method not in self.methods:
            return None
        if self.service is None:
            return {"ok":False, "error":"unavailable", "message":"Memory is unavailable."}
        try:
            if not isinstance(params, dict):
                raise MemoryRequestError("invalid_request")
            return await self.service.request(str(method.value if hasattr(method, "value") else method), params)
        except MemoryRequestError as exc:
            code = exc.code
        except Exception as exc:
            api = self.service._api
            if api is not None and isinstance(exc, api.MemoryConflict):
                code = "conflict"
                reason = str(exc) if str(exc) in {"forget_preview_stale", "forget_confirmation_required",
                    "forget_linked_scope_unavailable"} else None
                if reason is not None:
                    return {"ok":False, "error":code, "reason":reason,
                        "message":"Review the linked memories and confirm a fresh deletion preview."}
            elif api is not None and isinstance(exc, api.MemoryNotFound):
                code = "not_found"
            elif api is not None and isinstance(exc, api.MemoryValidationError):
                code = "invalid_request"
            else:
                logger.warning("[Memory] request unavailable (%s)", type(exc).__name__)
                code = "unavailable"
        return {"ok":False, "error":code, "message":{
            "conflict":"Memory changed or this operation key was reused. Refresh before editing.",
            "busy":"Memory is busy. Retry the same operation later.",
            "outcome_unknown":"The operation may still commit. Keep its operation ID and retry unchanged.",
            "context_changed":"Chat context changed. Refresh the memory panel.",
            "stale_session":"Open the current chat before using memory.",
            "not_found":"Memory is unavailable in this context.",
            "invalid_request":"The memory request is invalid.",
            "disabled":"Memory is disabled.",
        }.get(code, "Memory is unavailable.")}

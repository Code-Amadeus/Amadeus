workspace "Amadeus" "Current local AI OS interface architecture (observed 2026-07-22)" {
    !identifiers hierarchical

    model {
        user = person "User" "Speaks, types, reviews work, and authorizes bounded side effects."

        llmServices = softwareSystem "LLM Services" "Remote or local chat/reasoning model endpoints." "External"
        providerEngines = softwareSystem "Provider Engines" "Codex, OpenClaw, and browser execution kernels." "External"
        hostOs = softwareSystem "Host OS and Filesystem" "Windows desktop, project workspaces, audio devices, and external export targets." "External"

        amadeus = softwareSystem "Amadeus AI OS Interface" "Local conversational interface that routes work, preserves durable task state, narrates interventions, and projects evidence." {
            electron = container "Electron Desktop" "Durable chat, settings, provider controls, review surfaces, and backend lifecycle." "Electron / React / TypeScript" "Surface"
            wallpaper = container "Wallpaper and CRT Surface" "Ambient character presence, subtitles, compact work evidence, permission cards, and allowlisted actions." "Browser runtime / JavaScript" "Surface,UntrustedSurface"

            backend = container "Python Backend" "Headless orchestration, WebSocket API, conversation runtime, work control plane, providers, audio, and rendering bridges." "Python / FastAPI / asyncio" {
                composition = component "Application Composition" "Builds handlers, injects callbacks, starts services, and owns compatibility wiring." "server/app.py" "ConcentrationPoint"
                api = component "WebSocket and Request Handlers" "Dispatches protocol methods and forwards selected events." "server/ws_handler.py + server/handlers/*"
                eventBus = component "Event Bus" "In-process asynchronous pub/sub using protocol method names." "server/event_bus.py" "EventBus"

                chat = component "Chat and Turn Runtime" "Streams LLM output, parses structured tags, maintains conversation turns, and schedules sentence output." "core/chat_runtime.py + core/turn_coordinator.py"
                session = component "Session and Active Work Context" "Persists chat history and exposes bounded active-provider context to later turns." "core/session_manager.py + server/work_context.py"

                providerGateway = component "Provider Gateway" "Normalizes provider.run, cancellation, resume restrictions, and Provider inspection commands." "server/handlers/provider_handler.py"
                providerRuntime = component "Provider Runtime" "Owns normalized run lifecycle and emits provider.event/provider.result." "agent_host/provider_runtime.py" "CoreBoundary"
                adapters = component "Provider Adapters" "Translate Codex, OpenClaw, and browser engines into provider-neutral requests, events, and results." "agent_host/adapters/*" "Adapter"

                workControl = component "Work Ledger Control Plane" "Owns WorkItems, attempts, completion, focus, workspace leases, permissions, and selected-work projection." "server/work_ledger_coordinator.py" "CoreBoundary"
                workApi = component "Work Intent API" "Validates work actions, stale revisions, selected identity, retry/resume, disposition, and permission decisions." "server/handlers/work_ledger_handler.py"
                workActivity = component "Work Activity Projection" "Projects raw provider activity into canvas updates, character activity, semantic notes, and keypoints." "server/handlers/work_activity_handler.py" "MigrationSeam"
                observer = component "Observer and Narration Governor" "Coalesces semantic work notes and decides silent, canvas, subtitle, intermediate speech, permission prompt, or final report." "server/work_observer.py + server/work_narration_governor.py"
                canvasActions = component "Canvas Action Router" "Treats canvas actions as untrusted input and routes only allowlisted provider, work, and permission actions." "server/canvas_action_router.py" "SecurityBoundary"

                speech = component "Speech Runtime" "Coordinates TTS, playback ordering, ASR, wake, barge-in, and turn completion." "tts/* + asr/* + server/handlers/tts_handler.py"
                renderBridge = component "Character and Surface Bridges" "Projects subtitle, activity, canvas, expression, and SpriteForge intents to visible surfaces." "render/* + wallpaper/* + server/handlers/wallpaper_handler.py"
                secondaryModes = component "Secondary and Compatibility Modes" "VN Player, VTS, legacy GUI, and compatibility integrations." "vn_player/* + vts/* + main.py" "Compatibility"
            }

            ledger = container "Work Ledger" "Durable projects, WorkItems, attempts, artifacts, completion assessments, focus, leases, and permission requests." "SQLite" "Database"
        }

        user -> amadeus.electron "Chats, configures, starts work, and reviews results"
        user -> amadeus.wallpaper "Observes activity and invokes compact canvas actions"

        amadeus.electron -> amadeus.backend "Sends request envelopes and receives streamed events" "WebSocket / HTTP"
        amadeus.backend -> amadeus.wallpaper "Serves assets and pushes activity, canvas, subtitle, and character state" "Local bridge / HTTP"
        amadeus.wallpaper -> amadeus.backend "Returns allowlisted canvas intents" "Local bridge"
        amadeus.backend -> llmServices "Streams chat, observer, and translation requests" "HTTP / local process"
        amadeus.backend -> providerEngines "Starts and observes delegated execution" "CLI / SSE / gateway"
        amadeus.backend -> hostOs "Uses audio, windows, workspaces, git, and bounded file operations"
        amadeus.backend -> amadeus.ledger "Persists and projects durable work state" "SQLite"

        amadeus.backend.composition -> amadeus.backend.api "Constructs handlers and injects runtime callbacks"
        amadeus.backend.composition -> amadeus.backend.chat "Configures conversation and playback dependencies"
        amadeus.backend.composition -> amadeus.backend.providerGateway "Creates provider entry point"
        amadeus.backend.composition -> amadeus.backend.workControl "Creates store-backed control plane"
        amadeus.backend.composition -> amadeus.backend.observer "Connects busy-state, history, narration, and release callbacks"
        amadeus.backend.composition -> amadeus.backend.renderBridge "Starts visible runtime bridges"

        amadeus.backend.api -> amadeus.backend.chat "Dispatches chat.send/chat.abort"
        amadeus.backend.api -> amadeus.backend.providerGateway "Dispatches provider commands"
        amadeus.backend.api -> amadeus.backend.workApi "Dispatches work intents and permission decisions"
        amadeus.backend.api -> amadeus.backend.eventBus "Forwards selected runtime events"

        amadeus.backend.chat -> llmServices "Streams model responses"
        amadeus.backend.chat -> amadeus.backend.session "Reads and updates conversational state"
        amadeus.backend.chat -> amadeus.backend.providerGateway "Routes structured delegation"
        amadeus.backend.chat -> amadeus.backend.speech "Enqueues ordered sentence output"

        amadeus.backend.providerGateway -> amadeus.backend.workControl "Passes every new provider request through prepare_request"
        amadeus.backend.providerGateway -> amadeus.backend.providerRuntime "Starts, resumes, lists, or cancels normalized runs"
        amadeus.backend.providerRuntime -> amadeus.backend.adapters "Invokes adapter protocol"
        amadeus.backend.adapters -> providerEngines "Translates provider-specific execution streams"
        amadeus.backend.providerRuntime -> amadeus.backend.eventBus "Emits provider.event and provider.result"

        amadeus.backend.eventBus -> amadeus.backend.workControl "Delivers provider lifecycle and evidence"
        amadeus.backend.eventBus -> amadeus.backend.workActivity "Delivers provider activity for presentation"
        amadeus.backend.workControl -> amadeus.ledger "Persists authoritative work and permission facts"
        amadeus.backend.workControl -> amadeus.backend.eventBus "Emits work.updated, canvas projection, and semantic work notes"
        amadeus.backend.workActivity -> amadeus.backend.eventBus "Emits canvas, activity, character intent, and semantic work notes"
        amadeus.backend.eventBus -> amadeus.backend.observer "Delivers chat.work_note"
        amadeus.backend.observer -> amadeus.backend.session "Appends selected conclusions to main chat history"
        amadeus.backend.observer -> amadeus.backend.speech "Queues cadence-gated narration only when output is available"

        amadeus.wallpaper -> amadeus.backend.canvasActions "Submits untrusted canvas action payloads"
        amadeus.backend.canvasActions -> amadeus.backend.workApi "Routes revision-bound work and permission actions"
        amadeus.backend.canvasActions -> amadeus.backend.providerGateway "Routes allowlisted provider inspection actions"
        amadeus.backend.workApi -> amadeus.backend.workControl "Applies validated control-plane intent"

        amadeus.backend.eventBus -> amadeus.backend.renderBridge "Delivers activity, canvas, subtitle, and graph intent"
        amadeus.backend.speech -> hostOs "Captures and plays audio"
        amadeus.backend.speech -> amadeus.backend.renderBridge "Drives mouth, subtitle, speaking, and turn-complete state"
        amadeus.backend.renderBridge -> amadeus.wallpaper "Updates CRT and character scene"
        amadeus.backend.workControl -> hostOs "Manages workspace identity, writer leases, git evidence, and exact authorized exports"
        amadeus.backend.secondaryModes -> amadeus.backend.eventBus "Uses shared protocol events where integrated"
    }

    views {
        systemContext amadeus "system-context" {
            include *
            autoLayout lr
            description "Amadeus as the local AI OS interface between the user, models, execution providers, and the host OS."
        }

        container amadeus "runtime-containers" {
            include *
            autoLayout lr
            description "Current runtime containers and external dependencies."
        }

        component amadeus.backend "backend-components" {
            include *
            autoLayout lr
            description "Backend ownership boundaries, event flow, and current migration seams."
        }

        styles {
            element "Person" {
                shape Person
                background #08427b
                color #ffffff
            }
            element "Software System" {
                background #1168bd
                color #ffffff
            }
            element "Container" {
                background #438dd5
                color #ffffff
            }
            element "Component" {
                background #85bbf0
                color #111111
            }
            element "External" {
                background #777777
                color #ffffff
            }
            element "Database" {
                shape Cylinder
            }
            element "CoreBoundary" {
                background #2e7d32
                color #ffffff
            }
            element "MigrationSeam" {
                background #ef6c00
                color #ffffff
            }
            element "ConcentrationPoint" {
                background #6a1b9a
                color #ffffff
            }
            element "SecurityBoundary" {
                background #c62828
                color #ffffff
            }
            element "Compatibility" {
                background #757575
                color #ffffff
            }
            relationship "Relationship" {
                color #707070
            }
        }
    }

    configuration {
        scope softwaresystem
    }
}

// Generated from server/extensions/profile.json; do not edit.
export const extensionProfile = [
  {
    "kind": "voice_backend",
    "role": "tts",
    "tier": "process",
    "owner": "tts",
    "attach": "select",
    "bind_targets": [],
    "select_keys": [
      "TTS_BACKEND"
    ],
    "apply": "backend_restart",
    "status": "open",
    "reason": ""
  },
  {
    "kind": "mcp_server",
    "tier": "data",
    "owner": "mcp",
    "attach": "bind",
    "bind_targets": [
      "agents"
    ],
    "select_keys": [],
    "apply": "backend_restart",
    "status": "open",
    "reason": ""
  },
  {
    "kind": "skill",
    "tier": "data",
    "owner": "agent_skills",
    "attach": "bind",
    "bind_targets": [
      "agents"
    ],
    "select_keys": [],
    "apply": "backend_restart",
    "status": "open",
    "reason": ""
  },
  {
    "kind": "theme",
    "tier": "data",
    "owner": "desktop_theme",
    "attach": "select",
    "bind_targets": [],
    "select_keys": [
      "AMADEUS_UI_THEME"
    ],
    "apply": "frontend",
    "status": "open",
    "reason": ""
  },
  {
    "kind": "surface",
    "tier": "web",
    "owner": "extension_window",
    "attach": "present",
    "bind_targets": [],
    "select_keys": [],
    "apply": "frontend",
    "status": "closed",
    "reason": "Extension windows are planned for a later stage."
  },
  {
    "kind": "voice_backend",
    "role": "asr",
    "tier": "process",
    "owner": "asr",
    "attach": "select",
    "bind_targets": [],
    "select_keys": [],
    "apply": "backend_restart",
    "status": "closed",
    "reason": "ASR extensions are planned for a later stage."
  },
  {
    "kind": "auip_app",
    "tier": "web",
    "owner": "auip",
    "attach": "present",
    "bind_targets": [],
    "select_keys": [],
    "apply": "backend_restart",
    "status": "closed",
    "reason": "Installed AUIP applications require the application-source boundary."
  },
  {
    "kind": "context_source",
    "tier": "process",
    "owner": "context",
    "attach": "bind",
    "bind_targets": [],
    "select_keys": [],
    "apply": "backend_restart",
    "status": "closed",
    "reason": "Context and memory extensions are outside this pilot."
  },
  {
    "kind": "role_template",
    "tier": "data",
    "owner": "character",
    "attach": "select",
    "bind_targets": [],
    "select_keys": [],
    "apply": "backend_restart",
    "status": "closed",
    "reason": "Character extensions are outside this pilot."
  },
  {
    "kind": "visual_pack",
    "tier": "data",
    "owner": "visual",
    "attach": "select",
    "bind_targets": [],
    "select_keys": [],
    "apply": "frontend",
    "status": "closed",
    "reason": "Visual packs require an exported semantic binding contract."
  },
  {
    "kind": "observer",
    "tier": "process",
    "owner": "events",
    "attach": "bind",
    "bind_targets": [],
    "select_keys": [],
    "apply": "backend_restart",
    "status": "closed",
    "reason": "Observer extensions are outside this pilot."
  }
] as const

// Generated from config/catalog/**/*.json. Run npm run generate:config; do not edit.
import type { CatalogGroup } from './configCatalog.js'
export const catalogGroups: CatalogGroup[] = [
  {
    "id": "graphics_budget",
    "title": {
      "en-US": "Rendering budget",
      "zh-CN": "渲染设置"
    },
    "description": {
      "en-US": "Shared by character rendering and wallpaper surfaces.",
      "zh-CN": "角色渲染和壁纸画面共用这些设置。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "GRAPHICS_PROFILE": {
        "type": "enum",
        "title": {
          "en-US": "Graphics profile",
          "zh-CN": "图形预设"
        },
        "options": [
          {
            "value": "standard",
            "label": {
              "en-US": "Standard · 60 FPS, native pixel density",
              "zh-CN": "标准 · 60 FPS，原生像素密度"
            }
          },
          {
            "value": "power_saving",
            "label": {
              "en-US": "Power saving · 30 FPS, up to 1.5× pixel density",
              "zh-CN": "节能 · 30 FPS，像素密度最高 1.5×"
            }
          },
          {
            "value": "custom",
            "label": {
              "en-US": "Custom",
              "zh-CN": "自定义"
            }
          }
        ],
        "description": {
          "en-US": "Choose animation smoothness and GPU load. Custom values are preserved when using a preset.",
          "zh-CN": "按动画流畅度与 GPU 负载选择预设；切换预设不会清除自定义数值。"
        },
        "example_active": true,
        "default": "standard"
      },
      "RENDER_MAX_FPS": {
        "type": "integer",
        "title": {
          "en-US": "Frame-rate limit",
          "zh-CN": "帧率上限"
        },
        "min": 10,
        "max": 240,
        "step": 1,
        "description": {
          "en-US": "10–240 FPS. This is a ceiling, not a guaranteed frame rate.",
          "zh-CN": "范围 10–240 FPS。这是上限，不代表实际能达到的帧率。"
        },
        "example_active": true,
        "default": 30
      },
      "RENDER_MAX_RESOLUTION": {
        "type": "number",
        "title": {
          "en-US": "Pixel-density limit",
          "zh-CN": "像素密度上限"
        },
        "min": 0.25,
        "max": 4,
        "step": 0.25,
        "description": {
          "en-US": "0.25–4.0×, capped by the display’s native pixel density. Lower values reduce GPU work but may soften the image.",
          "zh-CN": "范围 0.25–4.0×，不超过显示器原生像素密度。调低可减少 GPU 负载，但画面可能变模糊。"
        },
        "example_active": true,
        "default": 1.5
      }
    }
  },
  {
    "id": "graphics_sampling",
    "title": {
      "en-US": "Texture loading",
      "zh-CN": "纹理加载"
    },
    "description": {
      "en-US": "Reduce repeated texture conversion while keeping the memory budget bounded.",
      "zh-CN": "在限制纹理内存预算的同时，减少重复转码。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "RENDER_TEXTURE_SAMPLING": {
        "type": "boolean",
        "title": {
          "en-US": "Sample animation textures",
          "zh-CN": "启用动画纹理采样"
        },
        "computed_default": true,
        "example": false,
        "description": {
          "en-US": "Skip source frames above the selected frame rate. On by default at 60 FPS; explicit choices are preserved. Restart the backend and reopen the character or wallpaper to apply.",
          "zh-CN": "跳过所选帧率下用不到的源帧。60 FPS 默认开启，保留手动选择。重启后端并重新打开角色或壁纸后生效。"
        }
      },
      "RENDER_BC7_CACHE": {
        "type": "boolean",
        "title": {
          "en-US": "Reuse converted textures on disk",
          "zh-CN": "复用磁盘中的已转码纹理"
        },
        "example": false,
        "description": {
          "en-US": "Builds a local cache as animations play, targeting 4 GiB of disk space. Reduces repeated conversion on supported wallpaper GPUs. Requires a backend restart.",
          "zh-CN": "随动画播放建立本地缓存，磁盘空间目标为 4 GiB。在支持的壁纸渲染设备上减少重复转码，重启后端后生效。"
        },
        "default": true
      }
    }
  },
  {
    "id": "tts_fish_audio",
    "title": {
      "en-US": "Fish Audio speech API",
      "zh-CN": "Fish Audio 语音 API"
    },
    "description": {
      "en-US": "WebSocket streaming speech with a hosted voice. Voice reference ID selects the voice; model selects the inference engine.",
      "zh-CN": "通过 WebSocket 流式生成语音；声音 ID 用于选择声音，模型用于选择推理引擎。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "FISH_TTS_WS_URL": {
        "type": "url",
        "title": {
          "en-US": "WebSocket URL",
          "zh-CN": "WebSocket 地址"
        },
        "default": "wss://api.fish.audio/v1/tts/live",
        "schemes": [
          "ws",
          "wss"
        ]
      },
      "FISH_TTS_API_KEY": {
        "type": "string",
        "title": {
          "en-US": "API key",
          "zh-CN": "API 密钥"
        },
        "secret": true
      },
      "FISH_TTS_MODEL": {
        "type": "string",
        "title": {
          "en-US": "Inference model",
          "zh-CN": "推理模型"
        },
        "default": "s2.1-pro-free"
      },
      "FISH_TTS_REFERENCE_ID": {
        "type": "string",
        "title": {
          "en-US": "Voice reference ID",
          "zh-CN": "声音 ID"
        },
        "default": "b450b19370434173b121446057622e9b"
      },
      "FISH_TTS_LATENCY": {
        "type": "enum",
        "title": {
          "en-US": "Latency mode",
          "zh-CN": "延迟模式"
        },
        "default": "balanced",
        "options": [
          "normal",
          "balanced",
          "low"
        ]
      }
    },
    "section": "remote",
    "voice_backend": {
      "id": "fish_audio",
      "label": {
        "en-US": "Fish Audio",
        "zh-CN": "Fish Audio"
      },
      "deployment": "remote",
      "factory": "tts.backends.fish_audio:FishAudioTTSBackend",
      "probe": "tts.backends.fish_audio:probe",
      "summary": "WebSocket text/audio streaming with a hosted voice reference.",
      "order": 3,
      "streaming": true,
      "reference_conditioning": false
    }
  },
  {
    "id": "tts_embedded_v3",
    "title": {
      "en-US": "Embedded GPT-SoVITS model",
      "zh-CN": "内置 GPT-SoVITS 模型"
    },
    "description": {
      "en-US": "Checkpoint pair for the Amadeus low-latency runtime. The SoVITS checkpoint header selects the v1, v2, v2Pro, v2ProPlus, or v3 decoder.",
      "zh-CN": "Amadeus 低延迟运行时使用的成对权重。SoVITS 权重文件头决定使用 v1、v2、v2Pro、v2ProPlus 或 v3 解码器。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "TTS_VOICE_PROFILE": {
        "type": "enum",
        "title": {
          "en-US": "Voice checkpoint profile",
          "zh-CN": "声音权重预设"
        },
        "options": [
          {
            "value": "kurisu_v3",
            "label": {
              "en-US": "Kurisu v3",
              "zh-CN": "红莉栖 v3"
            }
          },
          {
            "value": "kurisu_v2pro",
            "label": {
              "en-US": "Kurisu v2Pro · experimental",
              "zh-CN": "红莉栖 v2Pro · 实验性"
            }
          },
          {
            "value": "custom",
            "label": {
              "en-US": "Custom checkpoint pair",
              "zh-CN": "自定义权重组合"
            }
          }
        ],
        "description": {
          "en-US": "Named profiles select compatible GPT and SoVITS paths together. Restart the voice runtime after changing this setting.",
          "zh-CN": "命名预设会同时选择兼容的 GPT 与 SoVITS 路径；更改后需要重启语音运行时。"
        },
        "example": "kurisu_v3",
        "example_active": true,
        "default": "custom"
      },
      "TTS_DEVICE": {
        "type": "string",
        "title": {
          "en-US": "Inference device",
          "zh-CN": "推理设备"
        },
        "description": {
          "en-US": "auto/cuda, cuda:N, mps, or cpu.",
          "zh-CN": "auto/cuda、cuda:N、mps 或 cpu。"
        },
        "example": "auto",
        "example_active": true,
        "default": ""
      },
      "TTS_GPT_MODEL_PATH": {
        "type": "path",
        "title": {
          "en-US": "Custom GPT semantic checkpoint",
          "zh-CN": "自定义 GPT 语义权重"
        },
        "description": {
          "en-US": "Used only with the Custom checkpoint pair profile.",
          "zh-CN": "仅在选择“自定义权重组合”预设时使用。"
        },
        "example_active": true,
        "default": ""
      },
      "TTS_SOVITS_MODEL_PATH": {
        "type": "path",
        "title": {
          "en-US": "Custom SoVITS acoustic checkpoint",
          "zh-CN": "自定义 SoVITS 声学权重"
        },
        "description": {
          "en-US": "Used only with the Custom checkpoint pair profile.",
          "zh-CN": "仅在选择“自定义权重组合”预设时使用。"
        },
        "example_active": true,
        "default": ""
      }
    },
    "section": "output",
    "voice_backend": {
      "id": "gpt_sovits",
      "label": {
        "en-US": "GPT-SoVITS · Amadeus",
        "zh-CN": "GPT-SoVITS · Amadeus"
      },
      "deployment": "embedded",
      "factory": "tts.backends.gpt_sovits:GPTSoVITSBackend",
      "probe": "tts.backends.gpt_sovits:probe",
      "summary": "Amadeus low-latency runtime for v1, v2, v2Pro, v2ProPlus, and v3 checkpoints.",
      "order": 0,
      "streaming": true,
      "reference_conditioning": true
    }
  },
  {
    "id": "tts_mimo",
    "title": {
      "en-US": "MiMo speech API (Xiaomi)",
      "zh-CN": "MiMo 语音 API（小米）"
    },
    "description": {
      "en-US": "MiMo chat-completions synthesis with PCM16 SSE streaming.",
      "zh-CN": "使用 PCM16 SSE 流的 MiMo chat-completions 语音合成。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "MIMO_TTS_BASE_URL": {
        "type": "url",
        "title": {
          "en-US": "API base URL",
          "zh-CN": "API 基础地址"
        },
        "schemes": [
          "http",
          "https"
        ],
        "default": "https://api.xiaomimimo.com/v1"
      },
      "MIMO_TTS_API_KEY": {
        "type": "string",
        "title": {
          "en-US": "API key",
          "zh-CN": "API 密钥"
        },
        "secret": true
      },
      "MIMO_TTS_MODEL": {
        "type": "string",
        "title": {
          "en-US": "Model",
          "zh-CN": "模型"
        },
        "default": "mimo-v2.5-tts"
      },
      "MIMO_TTS_VOICE": {
        "type": "string",
        "title": {
          "en-US": "Voice",
          "zh-CN": "语音"
        },
        "default": "冰糖"
      }
    },
    "section": "remote",
    "voice_backend": {
      "id": "mimo",
      "label": {
        "en-US": "MiMo TTS (Xiaomi)",
        "zh-CN": "MiMo 语音合成（小米）"
      },
      "deployment": "remote",
      "factory": "tts.backends.mimo:MiMoTTSBackend",
      "probe": "tts.backends.mimo:probe",
      "summary": "MiMo chat-completions speech synthesis; PCM16 SSE streaming on mimo-v2.5-tts.",
      "order": 2,
      "streaming": true,
      "reference_conditioning": false
    }
  },
  {
    "id": "tts_remote",
    "title": {
      "en-US": "Remote speech API",
      "zh-CN": "远程语音合成 API"
    },
    "description": {
      "en-US": "OpenAI-compatible speech synthesis with buffered WAV or explicit SSE streaming.",
      "zh-CN": "兼容 OpenAI 的语音合成，支持缓冲 WAV 或显式 SSE 流式传输。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "TTS_API_BASE_URL": {
        "type": "url",
        "title": {
          "en-US": "API base URL",
          "zh-CN": "API 基础地址"
        },
        "schemes": [
          "http",
          "https"
        ],
        "default": "https://api.openai.com/v1"
      },
      "TTS_API_KEY": {
        "type": "string",
        "title": {
          "en-US": "API key",
          "zh-CN": "API 密钥"
        },
        "secret": true
      },
      "TTS_API_MODEL": {
        "type": "string",
        "title": {
          "en-US": "Model",
          "zh-CN": "模型"
        },
        "default": "gpt-4o-mini-tts"
      },
      "TTS_API_VOICE": {
        "type": "string",
        "title": {
          "en-US": "Voice",
          "zh-CN": "语音"
        },
        "default": "alloy"
      },
      "TTS_API_STREAM_PROTOCOL": {
        "type": "enum",
        "title": {
          "en-US": "Response mode",
          "zh-CN": "响应模式"
        },
        "options": [
          {
            "value": "buffered",
            "label": {
              "en-US": "Buffered WAV · compatible",
              "zh-CN": "缓冲 WAV · 兼容模式"
            }
          },
          {
            "value": "openai_sse",
            "label": {
              "en-US": "OpenAI SSE · streaming PCM",
              "zh-CN": "OpenAI SSE · 流式 PCM"
            }
          }
        ],
        "description": {
          "en-US": "Use OpenAI SSE only when the endpoint implements speech.audio.delta events.",
          "zh-CN": "仅在端点支持 speech.audio.delta 事件时使用 OpenAI SSE。"
        },
        "default": "buffered",
        "example": "openai_sse"
      }
    },
    "section": "remote",
    "voice_backend": {
      "id": "openai_compatible",
      "label": {
        "en-US": "OpenAI-compatible API",
        "zh-CN": "OpenAI 兼容 API"
      },
      "deployment": "remote",
      "factory": "tts.backends.openai_compatible:OpenAICompatibleTTSBackend",
      "probe": "tts.backends.openai_compatible:probe",
      "summary": "Buffered WAV compatibility or explicit OpenAI SSE first-packet playback.",
      "order": 1,
      "streaming": "tts.backends.openai_compatible:streaming_enabled",
      "reference_conditioning": false
    }
  },
  {
    "id": "speech_synthesis",
    "title": {
      "en-US": "Speech synthesis",
      "zh-CN": "语音合成"
    },
    "description": {
      "en-US": "The embedded default is Amadeus's low-latency GPT-SoVITS runtime and accepts v1, v2, v2Pro, v2ProPlus, and v3 checkpoints. Remote audio enters the same playback, subtitle, AEC, and mouth-signal pipeline.",
      "zh-CN": "默认使用 Amadeus 低延迟 GPT-SoVITS 运行时，支持 v1、v2、v2Pro、v2ProPlus 和 v3 权重。远程音频共用播放、字幕、回声消除和口型信号管线。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "TTS_BACKEND": {
        "type": "string",
        "title": {
          "en-US": "Backend",
          "zh-CN": "后端"
        },
        "example_active": true,
        "default": "gpt_sovits"
      }
    },
    "section": "output"
  }
]

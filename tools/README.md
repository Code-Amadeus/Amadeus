# Maintenance tools

Start with [CONTRIBUTING](../CONTRIBUTING.md). This index describes entry points;
it does not imply every historical smoke has been qualified on current main.
Modules may also be imported by tests or other tools.

- Deterministic checks use the locked L1 environment: `uv run --locked --no-sync python tools/run_tests.py`, `python -m ruff check .`, and `python tools/verify_python_environment.py --profile ci`.
- [maintainability_ratchet.py](maintainability_ratchet.py) scans source without models or Git metadata. `--update` only shrinks existing import/environment inventories. R3 retains the explicitly deferred VN context write; R4 checks runtime Python including GPT-SoVITS and excludes tests/tools.
- Live smoke/E2E scripts can start processes, connect to applications or call configured Providers. Read their module documentation and arguments before choosing a journey. They are not all part of the model-less suite.
- Audio/model tools require the corresponding optional installation profile and local assets. This index does not establish device or model qualification.
- [probes/](probes/) contains experimental evidence scripts. [architecture/](architecture/) owns generated architecture views. Bundled upstream helpers retain their provenance and optional dependencies.

Inspected on 2026-10-11. References identify guide/CI mentions, not successful
execution. Tools without current validation evidence remain unverified until
their specific journey is run.

## Documented maintenance entry points

| Tool | Purpose / entry note | Guide or CI reference |
| --- | --- | --- |
| [build_art_collection.py](build_art_collection.py) | Extend an existing art download collection with an independently optional Lite ZIP. | [docs/external_asset_bundles.md](../docs/external_asset_bundles.md) |
| [build_source_release.py](build_source_release.py) | Check and assemble a deterministic Amadeus public source archive. | [.github/workflows/source-release.yml](../.github/workflows/source-release.yml) |
| [character_rag.py](character_rag.py) | Build or inspect the optional character index: python -m tools.character_rag. | [.github/workflows/character-rag.yml](../.github/workflows/character-rag.yml); [docs/configuration_convergence_plan_2026-10-10.md](../docs/configuration_convergence_plan_2026-10-10.md) |
| [check_third_party_provenance.py](check_third_party_provenance.py) | Validate the checked-in third-party provenance inventory. | [.github/workflows/python-linux.yml](../.github/workflows/python-linux.yml); [.github/workflows/source-release.yml](../.github/workflows/source-release.yml) |
| [external_assets.py](external_assets.py) | Build, verify, and install separately distributed Amadeus runtime assets. | [README.md](../README.md); [README_ZH.md](../README_ZH.md) |
| [package_companion_character.py](package_companion_character.py) | Package approved, generated small portraits; no runtime authoring-workspace dependency. | [docs/companion-lite-2026-09-19.md](../docs/companion-lite-2026-09-19.md); [docs/external_asset_bundles.md](../docs/external_asset_bundles.md) |
| [revendor_pixi_basis_ktx2.py](revendor_pixi_basis_ktx2.py) | Rebuild the checked-in PixiJS KTX2 browser shim from pinned npm sources. | [docs/render_vendor_compatibility.md](../docs/render_vendor_compatibility.md) |
| [run_tests.py](run_tests.py) | Run isolated per-file Python suites. | [.github/workflows/python-windows.yml](../.github/workflows/python-windows.yml); [CONTRIBUTING.md](../CONTRIBUTING.md) |
| [run_wallpaper_engine_bridge.py](run_wallpaper_engine_bridge.py) | Diagnostic launcher for the current web Wallpaper Engine bridge. | [README.md](../README.md); [README_ZH.md](../README_ZH.md) |
| [validate_character_pack.py](validate_character_pack.py) | Validate one Amadeus runtime character pack without loading the renderer. | [docs/character_pack_authoring.md](../docs/character_pack_authoring.md) |
| [verify_clean_python_install.ps1](verify_clean_python_install.ps1) | See source for purpose and prerequisites; not independently qualified. | [docs/install_profiles.md](../docs/install_profiles.md) |
| [verify_python_environment.py](verify_python_environment.py) | Check that an installed environment matches an Amadeus release profile. | [.github/workflows/python-linux.yml](../.github/workflows/python-linux.yml); [.github/workflows/python-local-models.yml](../.github/workflows/python-local-models.yml) |
| [vn_portrait_overlay_lite.py](vn_portrait_overlay_lite.py) | Standalone VN portrait window using the bundled Companion Lite assets. | [docs/vn-shared-companion-2026-09-19.md](../docs/vn-shared-companion-2026-09-19.md); [docs/vn-text-sources.md](../docs/vn-text-sources.md) |
| [watch_chat_events.py](watch_chat_events.py) | See source for purpose and prerequisites; not independently qualified. | [docs/barge_in_aec_interrupt_notes.md](../docs/barge_in_aec_interrupt_notes.md) |

## Smoke, journey and evaluation tools

| Tool | Purpose / entry note | Guide or CI reference |
| --- | --- | --- |
| [e2e_auip_external_attach.py](e2e_auip_external_attach.py) | Run a real-WebSocket AUIP external-app simulation without opening a GUI. | No current guide/CI entry reference found. |
| [e2e_chain_report.py](e2e_chain_report.py) | Parse main app logs and report first-sentence E2E latency breakdown. | No current guide/CI entry reference found. |
| [e2e_codex_app_server_control.py](e2e_codex_app_server_control.py) | Exercise status and native steer through the real Amadeus Chat/Host path. | No current guide/CI entry reference found. |
| [e2e_codex_app_server_permission.py](e2e_codex_app_server_permission.py) | Verify Codex native approval -> Host allow/deny -> same-turn continuation. | No current guide/CI entry reference found. |
| [e2e_direct_codex_conversation.py](e2e_direct_codex_conversation.py) | Exercise a Codex transport through the normal Amadeus server/chat entrypoint. | No current guide/CI entry reference found. |
| [e2e_live_product_journey.py](e2e_live_product_journey.py) | Launch the shipping Electron product and drive one evidence-rich live Journey. | No current guide/CI entry reference found. |
| [e2e_real_work_conversation.py](e2e_real_work_conversation.py) | Run an isolated, real-model conversation through Amadeus and Codex. | No current guide/CI entry reference found. |
| [e2e_routing_matrix.py](e2e_routing_matrix.py) | Data-driven, isolated routing E2E testbed. | No current guide/CI entry reference found. |
| [e2e_work_acceptance.py](e2e_work_acceptance.py) | Automated P0 control-plane acceptance against a real backend process. | No current guide/CI entry reference found. |
| [e2e_work_preview.py](e2e_work_preview.py) | Run one real Electron Work Preview hot-reload journey. | No current guide/CI entry reference found. |
| [e2e_work_preview_auip_handoff.py](e2e_work_preview_auip_handoff.py) | Prove that a live Work Preview becomes an AUIP surface in-place. | No current guide/CI entry reference found. |
| [eval_codex_role_contract.py](eval_codex_role_contract.py) | Evaluate one persistent Codex Kurisu root over a long Amadeus conversation. | No current guide/CI entry reference found. |
| [eval_emo_history_ab.py](eval_emo_history_ab.py) | Measure whether model-visible EMO history reinforces later EMO output. | No current guide/CI entry reference found. |
| [eval_vn_player_longrun.py](eval_vn_player_longrun.py) | Long-run evaluation for Amadeus VN Player mode. | No current guide/CI entry reference found. |
| [eval_vn_player_reaction_gold.py](eval_vn_player_reaction_gold.py) | Compare VN Player actual reactions against designer/model ideal reactions. | No current guide/CI entry reference found. |
| [live_runtime_acceptance.py](live_runtime_acceptance.py) | Attach to an already-running Amadeus backend and record live evidence. | No current guide/CI entry reference found. |
| [run_semantic_journeys.py](run_semantic_journeys.py) | Run the implemented Alpha semantic Journeys without hiding missing ones. | No current guide/CI entry reference found. |
| [score_vn_player_golden.py](score_vn_player_golden.py) | Score a VN Player long-run report against a soft golden window. | No current guide/CI entry reference found. |
| [smoke_active_provider_context_prompt.py](smoke_active_provider_context_prompt.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_ai_os_browser_workflow_contract.py](smoke_ai_os_browser_workflow_contract.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_ai_os_interface_contract.py](smoke_ai_os_interface_contract.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_ai_os_runtime_schema.py](smoke_ai_os_runtime_schema.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_browser_branch_llm_dom_decision.py](smoke_browser_branch_llm_dom_decision.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_browser_branch_runtime_real_flow.py](smoke_browser_branch_runtime_real_flow.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_browser_canvas_flow.py](smoke_browser_canvas_flow.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_browser_interaction_policy_contract.py](smoke_browser_interaction_policy_contract.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_browser_interaction_refs.py](smoke_browser_interaction_refs.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_browser_planner_continuous_matrix.py](smoke_browser_planner_continuous_matrix.py) | Real Browser planner matrix against a deterministic local website. | No current guide/CI entry reference found. |
| [smoke_browser_provider_continuity.py](smoke_browser_provider_continuity.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_browser_real_scene_set.py](smoke_browser_real_scene_set.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_browser_runtime_semantics_matrix.py](smoke_browser_runtime_semantics_matrix.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_builtin_handler_catalog.py](smoke_builtin_handler_catalog.py) | Start an isolated, model-less backend and verify built-in factory registration. | [docs/configuration_convergence_plan_2026-10-10.md](../docs/configuration_convergence_plan_2026-10-10.md) |
| [smoke_canvas_action_router.py](smoke_canvas_action_router.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_canvas_bridge_auth_retry.py](smoke_canvas_bridge_auth_retry.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_delegate_stream_attrs.py](smoke_delegate_stream_attrs.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_diff_canvas_renderer.py](smoke_diff_canvas_renderer.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_electron_model_less.py](smoke_electron_model_less.py) | Exercise the shipping Electron shell without models, credentials, or assets. | [.github/workflows/python-windows.yml](../.github/workflows/python-windows.yml); [.github/workflows/source-release.yml](../.github/workflows/source-release.yml) |
| [smoke_main_browser_continuous_scene.py](smoke_main_browser_continuous_scene.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_openclaw_canvas_flow.py](smoke_openclaw_canvas_flow.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_openclaw_live_canvas_flow.py](smoke_openclaw_live_canvas_flow.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_provider_branch_merge_contract.py](smoke_provider_branch_merge_contract.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_task_dock_renderer.py](smoke_task_dock_renderer.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_vn_agent_websocket_bridge.py](smoke_vn_agent_websocket_bridge.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_vn_launch_manager.py](smoke_vn_launch_manager.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_vn_player_runtime.py](smoke_vn_player_runtime.py) | Smoke-test VN Player runtime. | No current guide/CI entry reference found. |
| [smoke_vn_player_verifier.py](smoke_vn_player_verifier.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_wallpaper_keyboard_sfx_contract.py](smoke_wallpaper_keyboard_sfx_contract.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [smoke_work_observer_lifecycle.py](smoke_work_observer_lifecycle.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |

## Audio, image and device helpers

| Tool | Purpose / entry note | Guide or CI reference |
| --- | --- | --- |
| [aec_offline_probe.py](aec_offline_probe.py) | Offline echo-cancellation probe for AEC debug captures. | No current guide/CI entry reference found. |
| [analyze_bucket_routing.py](analyze_bucket_routing.py) | Bucket routing analysis v2 — text-aware vs fixed-offset strategies. | No current guide/CI entry reference found. |
| [check_img_size.py](check_img_size.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [color_match_frames.py](color_match_frames.py) | Match frame colors to one or two reference images. | No current guide/CI entry reference found. |
| [detect_mouth_masks.py](detect_mouth_masks.py) | Detect sprite mouth regions automatically | No current guide/CI entry reference found. |
| [flash_kvcache_microbench.py](flash_kvcache_microbench.py) | Microbenchmarks for T2S decode attention shapes. | No current guide/CI entry reference found. |
| [list_microphones.py](list_microphones.py) | List and test microphone devices used by ASR/WakeService. | No current guide/CI entry reference found. |
| [t2s_speed_bench.py](t2s_speed_bench.py) | Quick T2S decoding throughput benchmark. | No current guide/CI entry reference found. |
| [ttfp_test.py](ttfp_test.py) | Benchmark first-sentence TTS latency across early-cut and BigVGAN kernel modes. | No current guide/CI entry reference found. |
| [tts_latency_report.py](tts_latency_report.py) | Parse app logs and report end-to-end first-sentence latency for Bedrock runs. | No current guide/CI entry reference found. |
| [tts_text_processor.py](tts_text_processor.py) | TTS 专用文本处理模块。 | No current guide/CI entry reference found. |
| [verify_flash_attention.py](verify_flash_attention.py) | Explicit FlashAttention import / NVIDIA kernel probe; no models or downloads. | [docs/torch27_candidates.md](../docs/torch27_candidates.md) |

## Bundled upstream helpers

| Tool | Purpose / entry note | Guide or CI reference |
| --- | --- | --- |
| [cmd-denoise.py](cmd-denoise.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [my_utils.py](my_utils.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [slice_audio.py](slice_audio.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [slicer2.py](slicer2.py) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [subfix_webui.py](subfix_webui.py) | Process some integers. | No current guide/CI entry reference found. |
| [text_utils.py](text_utils.py) | 纯文本解析工具集。 | No current guide/CI entry reference found. |

## Other maintainer tools

| Tool | Purpose / entry note | Guide or CI reference |
| --- | --- | --- |
| [audit_cu124_dependencies.py](audit_cu124_dependencies.py) | Audit the observed Windows/Python 3.12/cu124 environment without mutating it. | No current guide/CI entry reference found. |
| [codex_provider_auth.py](codex_provider_auth.py) | Print one Amadeus provider token for Codex command-backed authentication. | No current guide/CI entry reference found. |
| [first_sentence_cache_admin.bat](first_sentence_cache_admin.bat) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [first_sentence_cache_admin.py](first_sentence_cache_admin.py) | Inspect and repair the first-sentence exact-match audio cache. | No current guide/CI entry reference found. |
| [package_spriteforge_character.py](package_spriteforge_character.py) | Build an optional Amadeus KTX2 character pack from a SpriteForge workspace. | No current guide/CI entry reference found. |
| [prebuild_first_sentence_audio_cache.py](prebuild_first_sentence_audio_cache.py) | Prebuild exact-match audio cache for likely Kurisu first utterances. | No current guide/CI entry reference found. |
| [semantic_journey_evidence.py](semantic_journey_evidence.py) | Canonical evidence records for product-semantic journeys. | No current guide/CI entry reference found. |
| [semantic_release_gate.py](semantic_release_gate.py) | Summarize canonical semantic-journey evidence for an Alpha candidate. | No current guide/CI entry reference found. |
| [sync_auip_manifest.py](sync_auip_manifest.py) | Synchronize one validated AUIP manifest into an HTML entry document. | No current guide/CI entry reference found. |
| [test_discovery.py](test_discovery.py) | Make test discovery itself a checked repository invariant. | No current guide/CI entry reference found. |
| [validate_auip_entry.py](validate_auip_entry.py) | Boot one completed AUIP HTML entry in an isolated real browser page. | No current guide/CI entry reference found. |
| [validate_auip_manifest.py](validate_auip_manifest.py) | Validate an Amadeus AUIP v0 manifest. | No current guide/CI entry reference found. |
| [watch_chat_events.bat](watch_chat_events.bat) | See source for purpose and prerequisites; not independently qualified. | No current guide/CI entry reference found. |
| [ws_request.py](ws_request.py) | Send one backend WebSocket request from a batch file. | No current guide/CI entry reference found. |

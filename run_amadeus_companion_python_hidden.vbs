Set shell = CreateObject("WScript.Shell")
Set env = shell.Environment("PROCESS")
env("PYTHONUTF8") = "1"
env("PYTHONIOENCODING") = "utf-8"
env("PYTHONUNBUFFERED") = "1"
env("LANG") = "zh_CN.UTF-8"
env("OPEN_JTALK_DICT_DIR") = "C:\Users\violet\Desktop\Amadeus-Companion-Work\worktrees\integration\open_jtalk_dic\open_jtalk_dic_utf_8-1.11"
env("RAG_ENABLED_FOR_LOCAL") = "0"
env("VTS_ENABLED") = "0"
env("VTS_HEARTBEAT_ENABLED") = "0"
env("VTS_RECONNECT_ENABLED") = "0"
env("WAKE_ENABLED") = "0"
env("AEC_REALTIME_ENABLED") = "0"
env("AMADEUS_VISION_ENABLED") = "1"
env("AMADEUS_VISION_MODE") = "on_demand"
env("AMADEUS_VISION_SCOPE") = "read_window"
env("AMADEUS_VISION_PROVIDER") = "qwen"
env("AMADEUS_COMPANION") = "1"
env("TTS_BACKEND") = "gpt_sovits"
env("TTS_DEVICE") = "cuda"
env("TTS_OUTPUT_LANGUAGE") = "ja"
env("TTS_REF_AUDIO_JA") = "./assets/audio/reference/kurisu_reference.wav"
' Remote DashScope recognizer. The default is qwen3_asr, which loads the local
' Qwen3-ASR model; this launcher must not do that. Conversation backend only.
env("ASR_BACKEND") = "qwen_remote"
env("QWEN3_ASR_DEVICE") = "cpu"

root = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
logPath = root & "\runtime\companion\python-hidden.log"
stopScript = root & "\stop_amadeus_companion.ps1"
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & stopScript & """", 0, True
cmd = "cmd.exe /d /c """"D:\a\Amadeus\.venv_cu124\Scripts\python.exe"" -m server.app --port 17777 --companion >> """ & logPath & """ 2>&1"""
shell.Run cmd, 0, False
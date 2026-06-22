# Scripts

脚本按用途分组，根目录只保留最常用的测试入口。

- `run_tests.sh`: 项目单测和编译检查。
- `bootstrap_fedora_amd.sh`: Fedora + AMD/Vulkan 新机器部署主入口，串起 venv、模型、QwenTTS lab 和前端构建。
- `dev/`: 本地开发环境和通用模型下载。
- `qwen/`: Qwen3TTS 构建、benchmark、provider smoke、克隆音色预编码。
- `service/`: Linux systemd user service 与启动/停止脚本。
- `smoke/`: WebSocket realtime smoke tests。
- `windows/`: 旧 Windows/PowerShell 兼容脚本，当前阶段不再重点维护。

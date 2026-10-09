#!/bin/zsh
# PA Agent 启动脚本
#
# 作用：
#   1. 加载 ~/.okx_env 里的 OKX API 凭据（OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE）
#   2. 打印凭据是否就绪（只显示「已设置/未设置」，不打印任何密钥内容）
#   3. 显示出口 IP —— OKX 看到的就是这个 IP，必须和白名单里绑定的一致
#   4. 启动程序
#
# 用法：在终端里执行  ./run_pa_agent.sh   （或者双击本文件）

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$HOME/.okx_env" ]; then
  source "$HOME/.okx_env"
else
  echo "提示：未找到 ~/.okx_env，OKX 下单所需的三个环境变量将为空。"
fi

# OKX 白名单绑的是代理出口 IP：双击启动时终端可能没有代理变量，这里补上本机默认代理。
# （在终端里启动且已有代理变量时不会覆盖）
if [ -z "$HTTPS_PROXY" ]; then
  export HTTP_PROXY="http://127.0.0.1:10808"
  export HTTPS_PROXY="http://127.0.0.1:10808"
  export ALL_PROXY="socks5h://127.0.0.1:10808"
  export NO_PROXY="localhost,127.0.0.1,::1"
fi

echo "── OKX 交易凭据 ──────────────────────────────"
[ -n "$OKX_API_KEY" ]     && echo "  OKX_API_KEY      : 已设置" || echo "  OKX_API_KEY      : 未设置"
[ -n "$OKX_SECRET_KEY" ]  && echo "  OKX_SECRET_KEY   : 已设置" || echo "  OKX_SECRET_KEY   : 未设置"
[ -n "$OKX_PASSPHRASE" ]  && echo "  OKX_PASSPHRASE   : 已设置" || echo "  OKX_PASSPHRASE   : 未设置"

echo "── 网络出口（OKX 看到的来源 IP）──────────────"
OUT_IP="$(curl -s -m 8 https://api.ipify.org || true)"
echo "  出口 IP          : ${OUT_IP:-获取失败（检查代理是否在运行）}"
echo "  代理             : ${HTTPS_PROXY:-未设置}"
echo "─────────────────────────────────────────────"

# 后台（无界面）运行：默认不弹窗口，只跑监控/分析/下单。
# 需要看界面时用 PA_AGENT_HEADLESS=0 ./run_pa_agent.sh
if [ "${PA_AGENT_HEADLESS:-1}" = "1" ]; then
  export QT_QPA_PLATFORM=offscreen
  echo "运行模式         : 后台无界面（要看界面就设 PA_AGENT_HEADLESS=0）"
else
  echo "运行模式         : 前台带界面"
fi

cd "$PROJECT_DIR"
exec uv run python -m pa_agent.main

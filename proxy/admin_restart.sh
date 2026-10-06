#!/bin/bash
# 管理界面「重启服务」按钮的落点：proxy/admin_ui_daemon.py 检测到本文件存在时，
# 会把它导出成 FNMUSIC_ADMIN_RESTART_SCRIPT，admin_ui.py 再按需执行它。
#
# 为什么不是「只重启代理进程」：proxy/takeover.py 不监督子进程——uvicorn 一退出就
# 判定接管失败并回滚 socket，所以重启只能走 systemd。restart 会连带重启管理界面自己
# （它与主代理同在该单元的 cgroup 里），页面会断开几秒后自动恢复，这是预期行为。
#
# --no-block：立刻返回，让管理页的 HTTP 响应先发出去，免得请求被自己触发的重启掐断。
set -uo pipefail

UNIT="${FNMUSIC_SERVICE_UNIT:-fnmusic-ext.service}"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "未找到 systemctl，请手动重启 ${UNIT}" >&2
    exit 1
fi

if ! systemctl --no-block restart "${UNIT}"; then
    echo "重启 ${UNIT} 失败（需要 root 权限，或单元名不对？）" >&2
    exit 1
fi

echo "已触发 ${UNIT} 重启，新配置会在几秒内生效。"

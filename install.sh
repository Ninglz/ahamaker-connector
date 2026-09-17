#!/usr/bin/env bash
# 啊哈创作·企业公众号连接器 —— 一键安装脚本
#
# 用法（在企业的 Linux 服务器上以 root 执行安装，服务本身以专用低权限账号运行）：
#   curl -fsSL https://raw.githubusercontent.com/Ninglz/ahamaker-connector/main/install.sh -o install.sh
#   sudo bash install.sh --appid <公众号AppID> --secret <公众号AppSecret> [--author <作者名>] [--port 8807]
#
# 脚本会自动完成：
#   1. 创建专用系统账号 ahamaker（无登录 shell，服务不以 root 常驻）
#   2. 从远程镜像下载 wechat_connector.py（纯标准库，无需 pip）
#   3. 生成配置文件 /etc/ahamaker-connector.env（root:ahamaker 640；未提供令牌时自动生成）
#   4. 注册并启动 systemd 服务（User=ahamaker，开机自启，含基础加固）
#   5. 健康检查 + 自检
#   6. 打印「连接器地址 / 访问令牌 / 服务器公网 IP」——填回啊哈创作 App 即可
#
# 卸载：sudo bash install.sh --uninstall
set -euo pipefail

INSTALL_DIR="/opt/ahamaker-connector"
ENV_FILE="/etc/ahamaker-connector.env"
UNIT_FILE="/etc/systemd/system/ahamaker-connector.service"
SERVICE_NAME="ahamaker-connector"
SERVICE_USER="ahamaker"
PORT="8807"
APPID="" SECRET="" AUTHOR="" TOKEN=""
SCRIPT_URLS=(
  "https://raw.githubusercontent.com/Ninglz/ahamaker-connector/main/wechat_connector.py"
  "https://gitee.com/ninglz/ahamaker-connector/raw/main/wechat_connector.py"
)
# 可用环境变量 AHAMAKER_SCRIPT_URL 覆盖下载地址（如腾讯云 COS / 自建镜像）

msg()  { printf '\033[1;32m[安装]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[失败]\033[0m %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  systemctl disable "$SERVICE_NAME" 2>/dev/null || true
  rm -f "$UNIT_FILE" "$ENV_FILE"
  systemctl daemon-reload 2>/dev/null || true
  echo "已停止并移除 ahamaker-connector 服务；目录 $INSTALL_DIR 保留（可手动删除）；系统账号 $SERVICE_USER 保留（如需删除：userdel $SERVICE_USER）。"
  exit 0
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --appid)  APPID="$2";  shift 2 ;;
    --secret) SECRET="$2"; shift 2 ;;
    --author) AUTHOR="$2"; shift 2 ;;
    --token)  TOKEN="$2";  shift 2 ;;
    --port)   PORT="$2";   shift 2 ;;
    *) fail "未知参数：$1（支持 --appid --secret --author --token --port --uninstall）" ;;
  esac
done

# 交互模式：缺什么问什么；非交互（管道执行）必须带齐参数
if [ -t 0 ]; then
  [ -z "$APPID" ]  && read -r -p "公众号 AppID: " APPID
  [ -z "$SECRET" ] && read -r -sp "公众号 AppSecret（输入不回显）: " SECRET && echo
  [ -z "$AUTHOR" ] && read -r -p "默认作者名（可回车跳过）: " AUTHOR
fi
[ -n "$APPID" ]  || fail "缺少 --appid（交互模式下会提示输入）"
[ -n "$SECRET" ] || fail "缺少 --secret（交互模式下会提示输入）"

if [ "$(id -u)" -ne 0 ]; then fail "请用 root 执行：sudo bash install.sh ..."; fi
command -v systemctl >/dev/null || fail "未检测到 systemd，本脚本仅支持 systemd 系统（Ubuntu/Debian/CentOS 等）"
command -v python3  >/dev/null || fail "未检测到 python3，请先安装 Python 3.8+"

# 1) 专用低权限服务账号（连接器运行时不写任何文件，只读配置，无需 root 常驻）
NOLOGIN="$(command -v nologin || echo /usr/sbin/nologin)"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell "$NOLOGIN" "$SERVICE_USER" \
    || fail "创建系统账号 $SERVICE_USER 失败"
  msg "已创建专用系统账号 $SERVICE_USER（服务以该身份运行，不用 root）"
fi

# 2) 下载连接器脚本
mkdir -p "$INSTALL_DIR"
msg "下载 wechat_connector.py …"
DOWNLOADED=""
for url in "${AHAMAKER_SCRIPT_URL:-}" "${SCRIPT_URLS[@]}"; do
  [ -z "$url" ] && continue
  if curl -fsSL --retry 2 --connect-timeout 8 "$url" -o "$INSTALL_DIR/wechat_connector.py"; then
    DOWNLOADED="$url"; break
  fi
done
[ -n "$DOWNLOADED" ] || fail "所有下载地址均失败。可把 App 内置的 wechat_connector.py 手动上传到 $INSTALL_DIR/ 后重新运行本脚本。"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR"; chmod 644 "$INSTALL_DIR/wechat_connector.py"
msg "已从 ${DOWNLOADED%%https://*}… 获取脚本"

# 3) 配置文件（root 管理，服务账号只读）
if [ -z "$TOKEN" ]; then TOKEN="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"; fi
umask 077
cat > "$ENV_FILE" <<EOF
AHAMAKER_CONNECTOR_TOKEN=$TOKEN
AHAMAKER_CONNECTOR_PORT=$PORT
WXA_APPID=$APPID
WXA_SECRET=$SECRET
WXA_AUTHOR=$AUTHOR
EOF
chown "root:$SERVICE_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"
msg "配置已写入 $ENV_FILE（root:ahamaker 640，AppSecret 不落日志）"

# 4) systemd 服务（低权限账号 + 基础加固）
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=AhaMaker WeChat draft connector
After=network-online.target
Wants=network-online.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/env python3 $INSTALL_DIR/wechat_connector.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME" >/dev/null 2>&1 || systemctl restart "$SERVICE_NAME"
sleep 1
systemctl is-active --quiet "$SERVICE_NAME" || { journalctl -u "$SERVICE_NAME" -n 20 --no-pager; fail "服务启动失败，见上方日志"; }
msg "服务已启动（运行身份：$SERVICE_USER）并设置开机自启"

# 5) 健康检查 + 自检
HEALTH="$(curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/health" || true)"
echo "$HEALTH" | grep -q '"auth": *true' || fail "健康检查未通过：$HEALTH"
msg "健康检查通过（本地 $PORT 端口）"
python3 "$INSTALL_DIR/wechat_connector.py" --check && msg "自检通过"

# 6) 汇总输出
PUBLIC_IP="$(curl -fsS --connect-timeout 5 https://ifconfig.me 2>/dev/null || curl -fsS --connect-timeout 5 https://api.ipify.org 2>/dev/null || echo '（自动获取失败，请手动确认）')"
cat <<EOF

==============================================
  部署完成！把下面信息填回啊哈创作 App
----------------------------------------------
  连接器地址   : http://服务器公网IP:$PORT  （建议配置 Nginx + HTTPS 后换成 https:// 域名）
  访问令牌     : $TOKEN
  服务器公网 IP: $PUBLIC_IP

  最后一步：把公网 IP 加入微信开发者平台
  （developers.weixin.qq.com → 我的业务 → 公众号 →
  基础信息 → 开发密钥 → API IP 白名单）
==============================================
EOF

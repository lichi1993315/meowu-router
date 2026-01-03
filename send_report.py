import datetime
import json
import os
from zoneinfo import ZoneInfo

import httpx

# --- 配置区域 ---
# 飞书应用凭证
APP_ID = os.getenv("FEISHU_BOT_API_KEY", "").strip()
APP_SECRET = os.getenv("FEISHU_BOT_API_SECRET", "").strip()
# 接收消息的 ID (群ID或用户OpenID)
RECEIVE_ID = os.getenv("FEISHU_CHAT_ID", "").strip()
RECEIVE_ID_TYPE = os.getenv("FEISHU_RECEIVE_ID_TYPE", "chat_id").strip()

# Grafana 配置
GRAFANA_URL = os.getenv("GRAFANA_URL", "").strip()
GRAFANA_TOKEN = os.getenv("GRAFANA_TOKEN", "").strip()
# 你的仪表盘截图 URL (在 Grafana 面板点 Share -> Direct Link Rendered Image 复制链接)
# 记得加上 &width=1000&height=500 参数控制大小
DASHBOARD_IMAGE_URL = os.getenv("GRAFANA_RENDER_URL", "").strip()
GRAFANA_RENDER_PATH = os.getenv("GRAFANA_RENDER_PATH", "").strip()
GRAFANA_TIMEOUT = float(os.getenv("GRAFANA_TIMEOUT", "20"))
FEISHU_TIMEOUT = float(os.getenv("FEISHU_TIMEOUT", "20"))
TITLE_TZ = os.getenv("REPORT_TIMEZONE", "Asia/Shanghai")

# --- 功能函数 ---

def get_tenant_access_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {"app_id": APP_ID, "app_secret": APP_SECRET}
    with httpx.Client(timeout=FEISHU_TIMEOUT) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
    if data.get("code") != 0:
        print(f"Feishu Token Error: {data}")
        return None
    return data.get("tenant_access_token")

def build_grafana_render_url():
    if DASHBOARD_IMAGE_URL:
        return DASHBOARD_IMAGE_URL
    if not (GRAFANA_URL and GRAFANA_RENDER_PATH):
        return ""
    return f"{GRAFANA_URL.rstrip('/')}/{GRAFANA_RENDER_PATH.lstrip('/')}"

def download_grafana_image():
    render_url = build_grafana_render_url()
    if not render_url:
        print("Grafana render URL missing. Set GRAFANA_RENDER_URL or GRAFANA_RENDER_PATH.")
        return None
    headers = {"Authorization": f"Bearer {GRAFANA_TOKEN}"}
    with httpx.Client(timeout=GRAFANA_TIMEOUT) as client:
        response = client.get(render_url, headers=headers)
        if response.status_code == 200:
            return response.content
        print(f"Grafana Screenshot Failed: {response.status_code} {response.text}")
    return None

def upload_image_to_feishu(token, image_data):
    url = "https://open.feishu.cn/open-apis/im/v1/images"
    headers = {"Authorization": f"Bearer {token}"}
    # 飞书要求上传 multipart/form-data，且必须指定 image_type="message"
    files = {"image": ("report.png", image_data, "application/octet-stream")}
    data = {'image_type': 'message'}
    
    with httpx.Client(timeout=FEISHU_TIMEOUT) as client:
        response = client.post(url, headers=headers, files=files, data=data)
        response.raise_for_status()
        payload = response.json()
    if payload.get("code") != 0:
        print(f"Upload Image Failed: {payload}")
        return None
    return payload.get("data", {}).get("image_key")

def send_feishu_msg_to_topic_group(token, image_key):
    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={RECEIVE_ID_TYPE}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8"
    }

    # 获取当前日期作为标题
    try:
        tzinfo = ZoneInfo(TITLE_TZ)
    except Exception:
        tzinfo = ZoneInfo("Asia/Shanghai")
    today_str = datetime.datetime.now(tzinfo).strftime("%Y-%m-%d")
    title = f"📊 Grafana 数据报表 ({today_str})"

    # 构建富文本 (Post) 结构
    # 飞书 Post 结构比较深： content -> post -> zh_cn -> title + content(数组)
    post_content = {
        "zh_cn": {
            "title": title,
            "content": [
                [
                    {
                        "tag": "text",
                        "text": "以下是服务器今日早间快照，请查收：\n"
                    },
                    {
                        "tag": "img",
                        "image_key": image_key
                    },
                    {
                        "tag": "text",
                        "text": "\n(数据来源: "
                    },
                    {
                        "tag": "a",
                        "text": "Grafana Dashboard",
                        "href": "http://20.198.242.101:9001/d/llm-router-dashboard"
                    },
                    {
                        "tag": "text",
                        "text": ")"
                    }
                ]
            ]
        }
    }

    payload = {
        "receive_id": RECEIVE_ID,
        "msg_type": "post",  # 注意这里改成了 post
        "content": json.dumps(post_content) # content 必须是 JSON 字符串
    }

    with httpx.Client(timeout=FEISHU_TIMEOUT) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    print(f"Send Topic Result: {data}")

# --- 主流程 ---
if __name__ == "__main__":
    if not (APP_ID and APP_SECRET and RECEIVE_ID and GRAFANA_TOKEN):
        print("Missing required environment variables.")
        raise SystemExit(1)

    print("1. Getting Feishu Token...")
    token = get_tenant_access_token()
    
    print("2. Downloading Dashboard Image...")
    img_data = download_grafana_image()
    
    if token and img_data:
        print("3. Uploading Image to Feishu...")
        img_key = upload_image_to_feishu(token, img_data)
        
        if img_key:
            print(f"4. Sending Message (Key: {img_key})...")
            send_feishu_msg_to_topic_group(token, img_key)
        else:
            print("Failed to get image key.")
    else:
        print("Failed to get token or image data.")

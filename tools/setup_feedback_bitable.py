#!/usr/bin/env python3
"""Provision the feedback Base once; credentials are read from the server .env only."""
import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

API = "https://open.feishu.cn/open-apis"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    env_path = Path(args.env_file)
    values = dict(os.environ)
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.startswith("#"):
                values[key.strip()] = value.strip().strip('"').strip("'")
    token = ""

    def call(path, body, method="POST"):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(API + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                data = json.load(response)
        except HTTPError as error:
            data = json.loads(error.read())
        if data.get("code") != 0:
            # API error code and message only; never echo tokens or configuration.
            raise RuntimeError(f"Feishu error {data.get('code')}: {data.get('msg', '')}")
        return data

    data = call("/auth/v3/tenant_access_token/internal", {
        "app_id": values.get("FEISHU_BOT_API_KEY", ""), "app_secret": values.get("FEISHU_BOT_API_SECRET", "")})
    token = data["tenant_access_token"]
    prefix = "FEISHU_FEEDBACK_TEST" if args.test else "FEISHU_FEEDBACK"
    app = values.get(prefix + "_APP_TOKEN", "")

    def save(key, value):
        # Record each successful provisioning step so reruns resume without creating duplicate Bases.
        lines = env_path.read_text().splitlines() if env_path.exists() else []
        lines = [line for line in lines if not line.startswith(key + "=")]
        lines.append(key + "=" + value)
        env_path.write_text("\n".join(lines) + "\n")
        env_path.chmod(0o600)
        values[key] = value

    if not app:
        data = call("/bitable/v1/apps", {"name": "喵呜岛 Bug 反馈" + ("（测试）" if args.test else ""), "time_zone": "Asia/Shanghai"})
        app = data["data"]["app"]["app_token"]
        save(prefix + "_APP_TOKEN", app)
    table = values.get(prefix + "_TABLE_ID", "")
    if not table:
        fields = [{"field_name": name, "type": 1} for name in
                  ("反馈编号", "问题描述", "复现办法", "联系方式", "截图时间", "游戏版本", "平台", "场景", "分辨率", "玩家标识", "会话标识", "近期错误摘要", "负责人", "处理备注")]
        fields += [{"field_name": "截图", "type": 17},
                   {"field_name": "提交时间", "type": 5, "property": {"date_formatter": "yyyy/MM/dd HH:mm"}},
                   {"field_name": "处理状态", "type": 3, "property": {"options": [{"name": n} for n in ("待处理", "处理中", "已解决")]}},
                   {"field_name": "优先级", "type": 3, "property": {"options": [{"name": n} for n in ("低", "中", "高", "紧急")]}}]
        data = call(f"/bitable/v1/apps/{app}/tables", {"table": {"name": "玩家反馈", "default_view_name": "全部反馈", "fields": fields}})
        table = data["data"]["table_id"]
        save(prefix + "_TABLE_ID", table)
    fields = call(f"/bitable/v1/apps/{app}/tables/{table}/fields?page_size=100", None, "GET")["data"]["items"]
    for name in ("复现办法", "联系方式"):
        if not any(field["field_name"] == name for field in fields):
            call(f"/bitable/v1/apps/{app}/tables/{table}/fields", {"field_name": name, "type": 1})
    state_field = next(field for field in fields if field["field_name"] == "处理状态")
    views_url = f"/bitable/v1/apps/{app}/tables/{table}/views"
    views = call(views_url + "?page_size=100", None, "GET")["data"]["items"]
    for name in ("待处理", "按版本", "已解决"):
        view = next((v for v in views if v["view_name"] == name), None)
        if view is None:
            view = call(views_url, {"view_name": name, "view_type": "grid"})["data"]["view"]
        if name != "按版本":
            option = next(o["id"] for o in state_field["property"]["options"] if o["name"] == name)
            call(views_url + "/" + view["view_id"], {"property": {"filter_info": {
                "conjunction": "and", "conditions": [{"field_id": state_field["field_id"],
                    "operator": "is", "value": json.dumps([option])}]}}}, "PATCH")
    admin = values.get("FEISHU_ADMIN_ID", "")
    if admin:
        member_type = values.get("FEISHU_ADMIN_MEMBER_TYPE") or ("openid" if admin.startswith("ou_") else "openchat" if admin.startswith("oc_") else "userid")
        try:
            call(f"/drive/v1/permissions/{app}/members?type=bitable&need_notification=false",
                 {"member_type": member_type, "member_id": admin, "perm": "edit"})
        except RuntimeError as error:
            print("Collaboration permission still requires configuration: " + str(error))
    print(json.dumps({"base_url": "https://feishu.cn/base/" + app, "table_id": table,
                      "environment": "test" if args.test else "production"}, ensure_ascii=False))


if __name__ == "__main__":
    main()

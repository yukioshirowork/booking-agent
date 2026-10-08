"""予約受付エージェント本体。AI（OpenAI Responses API）にツールを持たせ、ツール呼び出しのループを回す。

使い方:
    export OPENAI_API_KEY=...
    export OPENAI_MODEL=...   # 使っているモデル名
    python agent.py          # ターミナルでチャット
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI

import db

MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna")
# 推論モデル用の設定。使うモデルが reasoning に対応していない場合は None にする
REASONING = {"effort": "low"}
WEEKDAYS = "月火水木金土日"


def system_prompt() -> str:
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    menus = "\n".join(f"- {mid}: {name}（{mins}分、{price:,}円）" for mid, (name, mins, price) in db.MENUS.items())
    return f"""あなたは「{db.SHOP['name']}」の予約受付スタッフです。お客さんとチャットで、予約の受付、確認、キャンセルをします。

今日は {now:%Y-%m-%d}（{WEEKDAYS[now.weekday()]}曜日）、現在時刻は {now:%H:%M}（日本時間）です。「明日」「来週の金曜」などの相対的な日付は、この日付を基準に YYYY-MM-DD に直してください。

営業時間: {db.SHOP['hours']}、定休日: {db.SHOP['closed']}
メニュー:
{menus}

進め方:
1. 予約には「お名前」「電話番号」「メニュー」「日付」「開始時刻」が必要です。足りない情報は聞いてください。
2. 空き状況は必ず check_availability で確認してください。推測で「空いています」と言ってはいけません。
3. create_booking を呼ぶ前に、日付（曜日つき）、時刻、メニュー、料金を復唱して、お客さんの「はい」を確認してください。
4. 希望の時間が埋まっていたら、同じ日の空いている時間を2〜3個提案してください。
5. キャンセルは、電話番号で予約を探し（find_bookings）、どの予約かを確認してから cancel_booking を呼んでください。

次の場合は自分で判断せず、handoff_to_owner で店主に引き継いでください:
- クレーム、返金、料金の交渉
- メニューにない施術や特別な要望
- アレルギー、肌トラブル、妊娠中など、健康や安全に関わる相談
- その他、あなたが確信を持って答えられないこと

返答は丁寧で短く。ツールの結果にない情報（空き状況、料金、予約番号）を作らないでください。"""


TOOLS = [
    {
        "name": "get_shop_info",
        "description": "店舗の住所、営業時間、定休日、電話番号、メニューと料金を返す。",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "check_availability",
        "description": "指定した日付とメニューで予約できる開始時刻の一覧を返す。空なら予約できない（満席か定休日）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "menu_id": {"type": "string", "enum": list(db.MENUS)},
            },
            "required": ["date", "menu_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "create_booking",
        "description": "予約を確定する。お客さんが内容に同意した後でだけ呼ぶこと。",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "phone": {"type": "string", "description": "ハイフンなしの数字"},
                "menu_id": {"type": "string", "enum": list(db.MENUS)},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "start_time": {"type": "string", "description": "HH:MM（24時間表記）"},
            },
            "required": ["customer_name", "phone", "menu_id", "date", "start_time"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "find_bookings",
        "description": "電話番号から、確定済みの予約を探す。",
        "input_schema": {
            "type": "object",
            "properties": {"phone": {"type": "string", "description": "ハイフンなしの数字"}},
            "required": ["phone"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "cancel_booking",
        "description": "予約をキャンセルする。予約番号と電話番号の両方が一致した場合だけキャンセルされる。",
        "input_schema": {
            "type": "object",
            "properties": {"booking_id": {"type": "integer"}, "phone": {"type": "string"}},
            "required": ["booking_id", "phone"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "handoff_to_owner",
        "description": "自分で対応すべきでない相談（クレーム、返金、健康に関わる相談、メニュー外の要望など）を店主に引き継ぐ。",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "enum": ["complaint", "refund", "health", "special_request", "other"]},
                "summary": {"type": "string", "description": "店主向けの要約（お客さんの名前と連絡先があれば含める）"},
            },
            "required": ["reason", "summary"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

# Responses API の形式に変換する（input_schema → parameters。function で包まず平らに書く）
OPENAI_TOOLS = [
    {"type": "function", "name": t["name"], "description": t["description"],
     "parameters": t["input_schema"], "strict": True}
    for t in TOOLS
]


def run_tool(conn, name: str, args: dict) -> str:
    """ツールを実行し、結果を JSON 文字列で返す。失敗したら ValueError を投げる。"""
    if name == "get_shop_info":
        result = {**db.SHOP, "menus": {k: {"name": n, "minutes": m, "price_yen": p} for k, (n, m, p) in db.MENUS.items()}}
    elif name == "check_availability":
        result = {"date": args["date"], "available_start_times": db.available_times(conn, args["date"], args["menu_id"])}
    elif name == "create_booking":
        result = db.create_booking(conn, args["customer_name"], args["phone"], args["menu_id"], args["date"], args["start_time"])
    elif name == "find_bookings":
        result = {"bookings": db.find_bookings(conn, args["phone"])}
    elif name == "cancel_booking":
        result = db.cancel_booking(conn, args["booking_id"], args["phone"])
    elif name == "handoff_to_owner":
        result = db.log_handoff(conn, args["reason"], args["summary"])
    else:
        raise ValueError(f"unknown tool: {name}")
    return json.dumps(result, ensure_ascii=False)


class BookingAgent:
    """会話の履歴を持ち、1回のお客さんの発言ごとにツール呼び出しのループを回す。"""

    def __init__(self, conn, client: OpenAI | None = None, verbose: bool = False):
        self.conn = conn
        self.client = client or OpenAI()
        # システムプロンプトは毎回 instructions で渡す（instructions はその1回のリクエストにだけ効くため）
        self.instructions = system_prompt()
        # 会話の履歴（Responses API の input 項目）。モデルの出力項目もそのまま追記していく
        self.messages: list = []
        self.verbose = verbose
        self.tool_log: list[dict] = []  # 評価用：どのツールがどの引数で呼ばれたか
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def send(self, user_text: str, max_steps: int = 10) -> str:
        self.messages.append({"role": "user", "content": user_text})
        for _ in range(max_steps):
            params = {"model": MODEL, "instructions": self.instructions, "input": self.messages, "tools": OPENAI_TOOLS}
            if REASONING:
                params["reasoning"] = REASONING
            response = self.client.responses.create(**params)
            self.usage["input_tokens"] += response.usage.input_tokens
            self.usage["output_tokens"] += response.usage.output_tokens
            self.messages.extend(response.output)

            if response.status == "incomplete":
                reason = response.incomplete_details.reason if response.incomplete_details else None
                if reason == "content_filter":
                    return "申し訳ありません、このご相談にはお答えできません。お店に直接お問い合わせください。"
                return "申し訳ありません、うまく応答できませんでした。もう一度お試しください。"
            if any(c.type == "refusal" for item in response.output if item.type == "message" for c in item.content):
                return "申し訳ありません、このご相談にはお答えできません。お店に直接お問い合わせください。"

            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                return response.output_text

            for call in calls:
                try:
                    args = json.loads(call.arguments)  # 引数は JSON 文字列で来る
                    self.tool_log.append({"name": call.name, "input": args})
                    content = run_tool(self.conn, call.name, args)
                except (ValueError, KeyError) as e:  # json.JSONDecodeError も ValueError の一種
                    content = f"エラー: {e}"
                if self.verbose:
                    print(f"{call.name}, processing...")
                # ツールの結果は call_name と対応づけて返す
                self.messages.append({"type": "function_call_output", "call_id": call.call_id, "output": content})
        return "申し訳ありません、処理が長くなりすぎました。お店に直接お問い合わせください。"


if __name__ == "__main__":
    agent = BookingAgent(db.connect(), verbose=True)
    print(f"{db.SHOP['name']} 予約チャット（終了は Ctrl+C）")
    try:
        while True:
            print("Bot:", agent.send(input("あなた: ")))
    except (KeyboardInterrupt, EOFError):
        print(f"\nトークン使用量: {agent.usage}")

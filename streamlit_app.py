# streamlit_app.py
import streamlit as st
import json, os, re, time, uuid
from datetime import datetime, timedelta, timezone
from collections import Counter
from supabase import create_client, Client
from openai import OpenAI

# -------------------- 設定 --------------------
st.set_page_config(page_title="Albert β", page_icon="🧭", layout="centered")
st.title("Albert β（教育支援AIコーチ）")

# Secrets
SB_URL = st.secrets.get("SUPABASE_URL")
SB_KEY = st.secrets.get("SUPABASE_ANON_KEY")
OPENAI_KEY = st.secrets.get("OPENAI_API_KEY")
OPENAI_MODEL = st.secrets.get("OPENAI_MODEL", "gpt-4o-mini")

if not all([SB_URL, SB_KEY, OPENAI_KEY]):
    st.error("⚠️ Secrets に SUPABASE_URL / SUPABASE_ANON_KEY / OPENAI_API_KEY を設定してください。")
    st.stop()

# Clients
sb: Client = create_client(SB_URL, SB_KEY)
oa = OpenAI(api_key=OPENAI_KEY)

# -------------------- 共通データ --------------------
GROUPED = {
  "子どもの力を信じる": ["子どもの主体性を育てたい","自信を育てたい","自分で選ばせたい"],
  "成長を支える関わり方": ["少し頑張れる課題を出したい","失敗を受け止めたい","プロセスを褒めたい"],
  "温かい人間関係": ["安心できる雰囲気をつくりたい","ありのままを受け入れたい","相手の話に耳を傾けたい"]
}
REASONS = ["抽象的すぎる","学年フィット不足","時間に合わない","準備物が不明","声かけが弱い","安全面が不十分","保護者対応が不足","根拠が薄い"]

SENSITIVE_KEYS = [
    "いじめ","いじ(め|り)","暴力","殴","蹴","排除","無視","仲間はずれ","脅",
    "金(を|の)要求","晒し","SNS","ネットいじめ","自傷","自殺","死にたい",
    "ハラスメント","性(的|被害)","体罰","恐喝","集団で","標的","陰口"
]
NG_PHRASES = [
    "その場で(謝|和解)させる","仲直りさせる","両者をすぐ対面させる",
    "被害者.*同席させる","全員で.*活動させる","我慢させる","許させる","被害者.*配慮なく"
]

DEFAULT_POLICY = {
  "tone": {
    "teacher": "最初にねぎらい→要点→次の一歩。断定/否定を避け選択肢で提案。",
    "parent":  "不安を下げる語彙。1行要約→丁寧文。家庭でできる観察/声かけを具体に。",
    "student_low":  "やさしい言葉、短い文、選べる言い方。",
    "student_high": "尊重の語彙。理由→やり方→選択肢。命令形は避ける。"
  },
  "must_include": ["生徒の安全最優先", "個の尊重", "記録と共有の手順に従う"],
  "avoid_phrases": ["その場で謝らせる","両者を即対面","恥をかかせる","我慢させる"],
  "phrasebook": {
    "teacher_open": "いつも本当におつかれさまです。状況を丁寧に見てこられたことが伝わってきました。",
    "parent_open":  "いつもご協力ありがとうございます。学校として丁寧に様子を見てまいります。",
    "ask_help":     "無理のない範囲で、次の点だけ一緒に見守っていただけますか。"
  },
  "value_mapping": {
    "子どもの主体性を育てたい": ["自律","選択肢提示","自己決定理論"],
    "自信を育てたい":           ["有能感","成功体験","形成的FB"],
    "自分で選ばせたい":          ["選択肢","関与","自己決定理論"],
    "少し頑張れる課題を出したい": ["最近接発達域","スモールステップ"],
    "失敗を受け止めたい":        ["安心","学びのやり直し","成長志向"],
    "プロセスを褒めたい":        ["形成的FB","努力の言語化"],
    "安心できる雰囲気をつくりたい": ["心理的安全","関係性"],
    "ありのままを受け入れたい":  ["受容","傾聴"],
    "相手の話に耳を傾けたい":    ["アクティブリスニング","共感"]
  }
}

# -------------------- 小ユーティリティ --------------------
def detect_sensitive(text:str)->bool:
    if not text: return False
    return any(re.search(p, text) for p in SENSITIVE_KEYS)

def violates_ng(text:str)->bool:
    if not text: return False
    return any(re.search(p, text) for p in NG_PHRASES)

def get_now()->str:
    return datetime.now(timezone.utc).isoformat()

# 認証トークンを維持
def get_sb_client_with_token()->Client:
    token = st.session_state.get("sb_token")
    cli = create_client(SB_URL, SB_KEY)
    if token: cli.auth.set_auth(token)
    return cli

# -------------------- 認証UI（メール+パスワード） --------------------
def auth_view():
    st.subheader("ログイン / 新規登録")
    tab1, tab2 = st.tabs(["ログイン", "新規登録"])
    with tab1:
        email = st.text_input("メールアドレス", key="login_email")
        pw = st.text_input("パスワード", type="password", key="login_pw")
        if st.button("ログイン"):
            res = sb.auth.sign_in_with_password({"email":email, "password":pw})
            if res.user:
                st.session_state["sb_token"] = res.session.access_token
                st.rerun()
            else:
                st.error("ログインに失敗しました。")
    with tab2:
        name = st.text_input("表示名（例：山田先生）", key="reg_name")
        email2 = st.text_input("メールアドレス（新規）", key="reg_email")
        pw2 = st.text_input("パスワード（新規）", type="password", key="reg_pw")
        if st.button("新規登録"):
            res = sb.auth.sign_up({"email":email2, "password":pw2})
            if res.user:
                # プロフィール行を作成
                cli = get_sb_client_with_token()  # 未ログインなので作成は後で更新でもOK
                st.success("登録しました。ログインしてください。")
            else:
                st.error("登録に失敗しました。")

# -------------------- プロファイル & 所属 取得/作成 --------------------
def ensure_profile_and_org():
    cli = get_sb_client_with_token()
    u = cli.auth.get_user()
    if not u or not u.user:
        return None, None, None

    auth_uid = u.user.id
    email = u.user.email

    # users（プロフィール）を upsert
    cli.table("users").upsert({"id":auth_uid, "email":email}).execute()

    # 既存所属を取得
    mem = cli.table("memberships").select("org_id, role, orgs(name)").eq("user_id", auth_uid).execute()
    rows = mem.data or []
    if rows:
        org_id = rows[0]["org_id"]
        role = rows[0]["role"]
        org_name = rows[0]["orgs"]["name"]
        return auth_uid, org_id, {"role":role, "org_name":org_name}

    # 所属がなければウィザード
    st.subheader("はじめての設定（組織の作成）")
    org_name = st.text_input("学校/塾名")
    if st.button("組織を作成して開始"):
        if not org_name:
            st.warning("名称を入力してください。"); st.stop()
        # orgs 作成
        org = cli.table("orgs").insert({"name":org_name, "created_by":auth_uid}).execute()
        org_id = org.data[0]["id"]
        # memberships 自分を admin で作成
        cli.table("memberships").insert({"org_id":org_id, "user_id":auth_uid, "role":"admin"}).execute()
        # org_policies を初期化
        cli.table("org_policies").insert({
            "org_id":org_id, "tone":DEFAULT_POLICY["tone"],
            "must_include":DEFAULT_POLICY["must_include"],
            "avoid_phrases":DEFAULT_POLICY["avoid_phrases"],
            "phrasebook":DEFAULT_POLICY["phrasebook"],
            "value_mapping":DEFAULT_POLICY["value_mapping"],
            "updated_by":auth_uid
        }).execute()
        st.success("組織を作成しました。次に進みます。")
        st.rerun()

    st.stop()

# -------------------- ポリシー編集（簡易） --------------------
def policy_editor(org_id):
    st.subheader("学校ポリシー（簡易）")
    cli = get_sb_client_with_token()
    pol = cli.table("org_policies").select("*").eq("org_id", org_id).execute().data
    if not pol:
        st.info("ポリシーが未設定です。初期値を作成します。")
        cli.table("org_policies").insert({
            "org_id":org_id, "tone":DEFAULT_POLICY["tone"],
            "must_include":DEFAULT_POLICY["must_include"],
            "avoid_phrases":DEFAULT_POLICY["avoid_phrases"],
            "phrasebook":DEFAULT_POLICY["phrasebook"],
            "value_mapping":DEFAULT_POLICY["value_mapping"]
        }).execute()
        pol = cli.table("org_policies").select("*").eq("org_id", org_id).execute().data
    p = pol[0]
    tone = p["tone"]; must = p["must_include"]; avoid = p["avoid_phrases"]; phrase = p["phrasebook"]; vm = p["value_mapping"]

    with st.expander("口調・フレーズ（必要に応じて編集）", expanded=False):
        teacher_open = st.text_input("先生への冒頭", phrase.get("teacher_open",""))
        parent_open  = st.text_input("保護者への冒頭", phrase.get("parent_open",""))
        if st.button("フレーズを保存"):
            phrase["teacher_open"]=teacher_open; phrase["parent_open"]=parent_open
            cli.table("org_policies").update({"phrasebook":phrase}).eq("id", p["id"]).execute()
            st.success("保存しました。")

# -------------------- 相談フォーム → 生成 → 保存 --------------------
def consult_and_generate(uid, org_id):
    st.subheader("相談")
    with st.form("albert_form"):
        c1, c2 = st.columns(2)
        grade = c1.selectbox("学年 / 年齢", ["","幼児","小1-2","小3-4","小5-6","中1-3","高1-3","大学・成人","その他"])
        scale = c2.selectbox("人数・規模", ["","個別","数人（2-5）","小グループ（6-10）","学級全体"])

        c3, c4 = st.columns(2)
        scene = c3.selectbox("場面", ["","授業中","授業準備・片付け","休み時間","HR・学活","行事","保護者対応","部活動","オンライン学習"])
        frequency = c4.selectbox("頻度", ["","初回","時々","継続的（週1-2）","慢性的（ほぼ毎回）"])

        c5, c6 = st.columns(2)
        urgency = c5.selectbox("緊急度", ["","低","中","高"])
        emotion = c6.selectbox("あなたの今の気持ち", ["","困惑","焦り","怒り","心配","無力感","期待","落ち着いている"])

        c7, c8 = st.columns(2)
        subject = c7.selectbox("教科", ["未指定","国語","算数/数学","理科","社会","英語","体育","音楽","美術/図工","技家","総合"])
        timebox = c8.selectbox("時間制約（実施可能目安）", ["未指定","~5分","~10分","~15分","~30分"])

        specificity = st.selectbox("具体度レベル", ["標準","高め（超具体）"])
        message = st.text_area("相談内容（できるだけ具体に）", height=110)
        attempts = st.text_area("これまで試したこと（100字以内）", height=70, max_chars=100)

        st.markdown("**あなたが大切にしていること（最大4つ）**")
        values = []
        for sec, opts in GROUPED.items():
            st.caption(sec)
            selected = st.multiselect("　", opts, key=f"vals_{sec}", label_visibility="collapsed")
            values.extend(selected)
        if len(values) > 4:
            st.warning("価値観は最大4つまでにしてください。")

        sensitive_flag = st.checkbox("センシティブ（いじめ/暴力/脅し/自傷示唆 等）の可能性")
        auto_sensitive = detect_sensitive(message)
        show_safety = sensitive_flag or auto_sensitive

        s_q1=s_q2=s_q3=""
        if show_safety:
            st.info("安全優先：被害が想定される子の曝露は避け、学校の報告手順に従ってください。")
            s_q1 = st.radio("Q1. 継続的な標的化や危害が現在ありますか？", ["はい","いいえ","不明"], horizontal=True)
            s_q2 = st.radio("Q2. 学校の定めた報告フローに報告済みですか？", ["はい","いいえ"], horizontal=True)
            s_q3 = st.radio("Q3. 被害が想定される子の安全確保は取れていますか？", ["はい","いいえ"], horizontal=True)

        submitted = st.form_submit_button("提案を生成")
        need_stop = any(not v for v in [grade, scale, scene, frequency, urgency, emotion]) or len(values)>4
        if submitted and need_stop:
            st.error("未入力の必須項目または価値観の選びすぎがあります。")
            submitted = False

    if not submitted:
        return

    # ポリシー取得
    cli = get_sb_client_with_token()
    pol = cli.table("org_policies").select("*").eq("org_id", org_id).execute().data[0]
    tone = pol["tone"]; phrase = pol["phrasebook"]
    must_include = "・".join(pol["must_include"])
    avoid_words  = "・".join(pol["avoid_phrases"])
    vmapping = pol["value_mapping"]

    vtags = []
    for v in values: vtags += vmapping.get(v, [])
    vtags = "｜".join(sorted(set(vtags))) if vtags else "（無し）"
    value_text = "\n".join([f"- {v}" for v in values]) if values else "（特に指定なし）"

    safety_block = ""
    needs_safety = show_safety
    if needs_safety:
        safety_block = f"""
【安全ガード】
- 被害側の曝露を避け、分離/見守り/記録/報告を優先。AIは独断で判断しない。
- “仲直り/その場での謝罪/即対面/被害者の同席強制/一斉の活動強制”は行わない。
- 具体行動は「誰が・どこで・何を・何分で・想定リスク・代替案」を明記。
- 安全確認: Q1={s_q1} / Q2={s_q2} / Q3={s_q3}
"""

    fewshot = """
【例】
相談: 授業中に立ち歩く小2男子がいる
価値観: 子どもの主体性 / 安心
回答:
0) 先生へのひと言
- ここまで丁寧に見てこられたこと自体が土台です。短い一歩から一緒に整えましょう。
① 背景（理論タグ）
- 自席維持が難しい場合「注目の獲得」「体幹/感覚の欲求」が混在します。【根拠: PBIS】
② 明日ためせる行動レシピ
- 役割(プリント配り)を固定→成功を言語化【根拠: PBIS】
- 15分毎ストレッチを全体で導入【根拠: タイムオンタスク】
- 授業前30秒で役割予告【根拠: 前方支援】
③ 保護者への伝え方
- 1行要約＋丁寧文（家庭の観察ポイントを1つ）
④ 子どもへの声かけ（低/高）
- 「次はどれからやってみる？」/「どっちで進めるのがやりやすい？」
⑤ 成功の観察指標
- 立ち歩きの回数/役割の完了回数
⑥ 注意とフォロー
- 罰の席替えは逆効果。役割は更新。
"""
    theory_catalog = """
【理論候補】
- 自己決定理論（Deci & Ryan）/ 形成的フィードバック（Black & Wiliam）
- 最近接発達域・協同（Vygotsky）/ 認知負荷（Sweller）/ ワーキングメモリ（Baddeley）
- スモールステップ・強化（Skinner）/ タイムオンタスク / PBIS
"""

    prompt = f"""
あなたは教育支援AI「Albert」。先生を支え、子どもの成長と保護者の安心を後押しし、学校の価値観に合う提案だけを返します。
出力は「ねぎらい→具体策→言い換え（保護者/生徒）→観察→注意」。

【学校ポリシー（要反映）】
- 必須: {must_include}
- 避ける言い回し: {avoid_words}
- フレーズ集: 先生冒頭「{phrase.get('teacher_open','')}」/ 保護者冒頭「{phrase.get('parent_open','')}」
- トーン: 先生={tone.get('teacher','')} / 保護者={tone.get('parent','')} / 低学年={tone.get('student_low','')} / 中高生={tone.get('student_high','')}

{safety_block}

【与件】
- 価値観：
{value_text}
（価値観タグ）{vtags}
- 対象：{grade} / 教科：{subject} / 規模・場面：{scene} / 頻度：{frequency} / 緊急度：{urgency}
- 教師の感情：{emotion}
- 既試行策：{attempts if attempts else "（未記入）"}
- 相談内容：「{message}」
- 時間制約目安：{timebox}

【理論候補】{theory_catalog}

【出力形式（順番厳守 / 900〜1,200字）】
0) 先生へのひと言（30〜60字）
① 背景の見立て（理論タグ1つ）【根拠: 理論名/研究者】
② 明日ためせる行動レシピ ×3
  必須: 目的 / 適用条件（学年・場面・所要・準備物）/ 手順（3〜5）/ 声かけ例 / 代替案 / 失敗時の一手 / 観察指標 / 【根拠】 / 価値観タグ
③ 保護者への伝え方（1行要約＋丁寧文＋家庭での観察1つ）
④ 子どもへの声かけ（低学年/中高生の2パターン）
⑤ 成功の観察指標（2つ）
⑥ 注意とフォロー（安全最優先）

【厳守ルール】
- {timebox} の範囲で実施可能。抽象で終わらず、数値・固有名詞・具体動作を入れる。
- 学校ポリシーに反する提案はしない（避け語は出力に含めない）。
- 思考過程は出さない。最終出力のみ。
"""
    if specificity == "高め（超具体）":
        prompt += "\n【追加制約】各レシピは60〜120字で具体化。固有名詞・数値・具体動作を必ず含める。\n"

    with st.spinner("生成中..."):
        r = oa.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"system","content": fewshot + "\n" + prompt}],
            temperature=0.45, max_tokens=1200
        )
        text = r.choices[0].message.content
        if needs_safety and violates_ng(text):
            fix = "【修正指示】安全最優先・分離と見守り・記録と報告を前提に、被害側の曝露を避け、個別/環境調整中心で再提案。"
            r2 = oa.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role":"system","content": fewshot + "\n" + prompt + "\n" + fix}],
                temperature=0.4, max_tokens=1200
            )
            text = r2.choices[0].message.content

    # 相談保存 → 回答保存
    topics = []
    rules = [
        (r"いじめ|暴力|脅|自傷|自殺|安全|被害", "安全/人間関係"),
        (r"保護者|家庭|連絡|面談", "保護者対応"),
        (r"提出|宿題|課題|忘れ|未提出", "課題・提出"),
        (r"遅刻|欠席|不登校|登校しぶり", "出欠"),
        (r"集中|立ち歩き|私語|規律|荒れ", "授業規律"),
        (r"評価|テスト|成績|アセスメント", "評価"),
        (r"友だち|仲間|グループ|孤立", "関係性"),
    ]
    for pat, tag in rules:
        if re.search(pat, message): topics.append(tag)
    if not topics: topics = ["未分類"]

    cli.table("consultations").insert({
        "org_id": org_id, "user_id": uid, "grade": grade, "scale": scale, "scene": scene,
        "frequency": frequency, "urgency": urgency, "emotion": emotion, "subject": subject,
        "timebox": timebox, "specificity": specificity, "message": message, "attempts": attempts,
        "values": values, "sensitive_flag": needs_safety,
        "safety_answers": {"q1": s_q1, "q2":s_q2, "q3":s_q3} if needs_safety else None,
        "topics": topics
    }).execute()

    # 最新相談を取得（今挿したもの）
    cons = cli.table("consultations").select("id").eq("user_id", uid).order("created_at", desc=True).limit(1).execute().data[0]
    cid = cons["id"]

    cli.table("answers").insert({
        "consultation_id": cid, "model": OPENAI_MODEL, "safety_mode": needs_safety,
        "text": text
    }).execute()

    st.caption("この入力で生成： " + " / ".join([x for x in [grade, scene, timebox, urgency, emotion] if x]))
    st.markdown(text)

    # フィードバック
    with st.expander("しっくりきませんか？ フィードバック"):
        c1, c2 = st.columns([1,2])
        rating = c1.radio("役立ち度", ["good","ok","bad"], horizontal=True, index=1)
        reasons = c2.multiselect("不足していた点（複数可）", REASONS)
        note = st.text_input("メモ（任意）")
        if st.button("フィードバックを保存"):
            ans = cli.table("answers").select("id").eq("consultation_id", cid).order("created_at", desc=True).limit(1).execute().data[0]
            cli.table("feedbacks").insert({
                "answer_id": ans["id"], "org_id": org_id, "user_id": uid,
                "rating": rating, "reasons": reasons, "note": note
            }).execute()
            st.success("保存しました。次回以降の最適化に使われます。")

# -------------------- ダッシュボード（最小KPI） --------------------
def dashboard(org_id):
    st.subheader("ダッシュボード（β・最小）")
    cli = get_sb_client_with_token()
    # 期間フィルタ：既定28日
    days = st.selectbox("期間", [7,28,90], index=1)
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()

    cons = cli.table("consultations").select("*").eq("org_id", org_id).gte("created_at", since).execute().data or []
    answers = cli.table("answers").select("id, consultation_id, created_at").execute().data or []
    fbs = cli.table("feedbacks").select("*").eq("org_id", org_id).gte("created_at", since).execute().data or []

    # 1) 行動実行率
    teacher_count = len(set([c["user_id"] for c in cons])) or 1
    weeks = max(days/7.0, 1.0)
    action_rate = round(len(cons)/teacher_count/weeks, 2)

    # 2) Helpful率
    helpful = [fb for fb in fbs if fb["rating"]=="good"]
    helpful_rate = round(100*len(helpful)/max(len(fbs),1), 1)

    # 3) 再生成率（粗く：feedbackメモに「再」など/将来は別カラム）
    regen_rate = round(100*len([fb for fb in fbs if "再" in (fb.get("note") or "")])/max(len(answers),1), 1)

    # 4) 時間適合率（理由に「時間に合わない」を含まない割合）
    bad_time = sum([1 for fb in fbs if "時間に合わない" in (fb.get("reasons") or [])])
    time_fit_rate = round(100*(1 - bad_time/max(len(fbs),1)), 1)

    # 5) センシティブ比率
    sens = sum([1 for c in cons if c["sensitive_flag"]])
    sens_rate = round(100*sens/max(len(cons),1), 1)

    # 6) ポリシー整合率（β：避け語が本文に出ていない割合）
    pol = cli.table("org_policies").select("avoid_phrases").eq("org_id", org_id).execute().data[0]
    avoid = pol["avoid_phrases"] or []
    def violates(text):
        return any(w in (text or "") for w in avoid)
    bad_policy = 0
    for a in answers:
        # 本来は join するが簡易に最新N件を対象に
        at = cli.table("answers").select("text").eq("id", a["id"]).execute().data[0]["text"]
        if violates(at): bad_policy += 1
    policy_ok_rate = round(100*(1 - bad_policy/max(len(answers),1)), 1)

    c1,c2,c3 = st.columns(3)
    c1.metric("行動実行率 / 週（主KPI）", action_rate)
    c2.metric("Helpful率（%）", helpful_rate)
    c3.metric("再生成率（%）", regen_rate)
    c4,c5,c6 = st.columns(3)
    c4.metric("時間適合率（%）", time_fit_rate)
    c5.metric("センシティブ比率（%）", sens_rate)
    c6.metric("ポリシー整合率（%）", policy_ok_rate)

    # 簡易：トピック分布
    topics = Counter()
    for c in cons:
        for t in (c.get("topics") or []):
            topics[t]+=1
    if topics:
        st.write("**トピック件数（上位）**")
        for k,v in topics.most_common(10):
            st.write(f"- {k}: {v}")

# -------------------- メイン制御 --------------------
def main():
    # 認証済判定
    token = st.session_state.get("sb_token")
    if not token:
        auth_view()
        return

    # プロファイルと所属チェック（なければ作成ウィザードへ）
    uid, org_id, meta = ensure_profile_and_org()
    st.sidebar.success(f"{meta['org_name']}（{meta['role']}）としてログイン中")
    if st.sidebar.button("ログアウト"):
        st.session_state.clear(); st.rerun()

    # タブ：相談 / ダッシュボード / 設定
    tab = st.sidebar.radio("メニュー", ["相談","ダッシュボード","設定"])

    if tab == "相談":
        consult_and_generate(uid, org_id)
    elif tab == "ダッシュボード":
        dashboard(org_id)
    else:
        policy_editor(org_id)
        st.info("※ 詳細な管理画面は今後拡充します。")

main()

# streamlit_app.py
import streamlit as st
import json, os, re, uuid, time
from collections import Counter
from openai import OpenAI

# ====== 画面設定 ======
st.set_page_config(page_title="Albert β", page_icon="🧭", layout="centered")
st.title("Albert β（教育支援AIコーチ）")
st.caption("価値観×状況に沿って、明日ためせる具体策＋根拠で支援します。")

# ====== OpenAI クライアント ======
API_KEY = st.secrets.get("OPENAI_API_KEY")
MODEL   = st.secrets.get("OPENAI_MODEL", "gpt-4o-mini")  # 例: gpt-4o-mini / gpt-4.1-mini 等
if not API_KEY:
    st.error("⚠️ StreamlitのSecretsに OPENAI_API_KEY を設定してください（[Manage app → Settings → Secrets]）。")
    st.stop()
client = OpenAI(api_key=API_KEY)

# ====== 保存先（βはJSONファイルに簡易保存） ======
MEM_PATH    = "albert_memory.json"    # フィードバック学習
EVENTS_PATH = "albert_events.json"    # 相談ログ（匿名集計）
POLICY_PATH = "albert_policy.json"    # 学校ポリシー（同梱 or 後から編集）

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ====== ポリシー（なければ最小デフォルトを生成） ======
DEFAULT_POLICY = {
  "school_name": "デフォルト校",
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
if not os.path.exists(POLICY_PATH):
    save_json(POLICY_PATH, DEFAULT_POLICY)
policy = load_json(POLICY_PATH, DEFAULT_POLICY)

# ====== 価値観カテゴリ ======
GROUPED = {
  "子どもの力を信じる": ["子どもの主体性を育てたい","自信を育てたい","自分で選ばせたい"],
  "成長を支える関わり方": ["少し頑張れる課題を出したい","失敗を受け止めたい","プロセスを褒めたい"],
  "温かい人間関係": ["安心できる雰囲気をつくりたい","ありのままを受け入れたい","相手の話に耳を傾けたい"]
}
REASONS = ["抽象的すぎる","学年フィット不足","時間に合わない","準備物が不明","声かけが弱い","安全面が不十分","保護者対応が不足","根拠が薄い"]

# ====== センシティブ検出/NG表現 ======
SENSITIVE_KEYS = [
    "いじめ","いじ(め|り)","暴力","殴","蹴","排除","無視","仲間はずれ","脅",
    "金(を|の)要求","晒し","SNS","ネットいじめ","自傷","自殺","死にたい",
    "ハラスメント","性(的|被害)","体罰","恐喝","集団で","標的","陰口"
]
NG_PHRASES = [
    "その場で(謝|和解)させる","仲直りさせる","両者をすぐ対面させる",
    "被害者.*同席させる","全員で.*活動させる","我慢させる","許させる","被害者.*配慮なく"
]
def detect_sensitive(text: str) -> bool:
    if not text: return False
    return any(re.search(p, text) for p in SENSITIVE_KEYS)
def violates_ng(text: str) -> bool:
    if not text: return False
    return any(re.search(p, text) for p in NG_PHRASES)

# ====== 個別化ヒント（フィードバックから） ======
def build_personalization(feedbacks: list) -> str:
    if not feedbacks: return ""
    val = Counter(); spec = Counter(); timeb = Counter(); subj = Counter(); bad = Counter()
    for fb in feedbacks:
        ui = fb.get("input", {})
        rt = fb.get("rating")
        if rt in ("good","ok"):
            for v in ui.get("values", []): val[v]+=1
            spec[ui.get("specificity","")] += 1
            timeb[ui.get("timebox","")]    += 1
            subj[ui.get("subject","")]     += 1
        if rt == "bad":
            for r in fb.get("reasons", []): bad[r]+=1
    def top2(c): return "・".join([k for k,_ in c.most_common(2) if k and k!="未指定"])
    lines=[]
    for label,cnt in [("好まれる具体度",spec),("よく選ぶ時間枠",timeb),("大切にされやすい価値観",val),("教科の傾向",subj),("避けたい傾向",bad)]:
        t=top2(cnt)
        if t: lines.append(f"- {label}: {t}")
    return "【個別化ヒント】\n" + "\n".join(lines) + "\n" if lines else ""

# ====== タイプライタ（擬似段階表示） ======
def typewriter(md_container, text: str, step=0.02):
    buf = ""
    for i in range(0, len(text), 80):
        buf = text[:i+80]
        md_container.markdown(buf)
        time.sleep(step)

st.write("—")

# ====== 入力フォーム ======
with st.form("albert_form", clear_on_submit=False):
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

    message = st.text_area("相談内容（状況をできるだけ具体的に）", height=110,
                           placeholder="例）小2の算数で立ち歩きが増加。席後方から前に移動して声かけるも効果薄…")
    attempts = st.text_area("これまで試したこと（100字以内）", height=70, max_chars=100,
                            placeholder="例）席替え、注意、係活動の導入 など")

    st.markdown("**あなたが大切にしていること（最大4つまで）**")
    values = []
    for sec, opts in GROUPED.items():
        st.caption(sec)
        selected = st.multiselect("　", opts, key=f"vals_{sec}", label_visibility="collapsed")
        values.extend(selected)
    if len(values) > 4:
        st.warning("価値観は最大4つまでにしてください。")

    sensitive_flag = st.checkbox("センシティブ（いじめ/暴力/脅し/自傷示唆 等）に該当する可能性がある")
    auto_sensitive = detect_sensitive(message)
    show_safety = sensitive_flag or auto_sensitive

    if show_safety:
        st.info("安全優先：被害が想定される子の曝露は避け、学校の報告手順に従ってください。")
        s_q1 = st.radio("Q1. 継続的な標的化や危害が現在ありますか？", ["はい","いいえ","不明"], horizontal=True)
        s_q2 = st.radio("Q2. 学校の定めた報告フローに報告済みですか？", ["はい","いいえ"], horizontal=True)
        s_q3 = st.radio("Q3. 被害が想定される子の安全確保は取れていますか？", ["はい","いいえ"], horizontal=True)
    else:
        s_q1=s_q2=s_q3=""

    submitted = st.form_submit_button("提案を生成")
    need_stop = any(not v for v in [grade, scale, scene, frequency, urgency, emotion]) or len(values)>4
    if submitted and need_stop:
        st.error("未入力の必須項目または価値観の選びすぎがあります。")
        submitted = False

# ====== 生成処理 ======
if submitted:
    # 個別化ヒント（直近フィードバックから）
    mem = load_json(MEM_PATH, {"users":{}})
    uid = st.session_state.get("uid") or uuid.uuid4().hex
    st.session_state["uid"] = uid
    feedbacks = mem.get("users", {}).get(uid, {}).get("feedback", [])
    personalization = build_personalization(feedbacks)

    # ポリシー抽出
    tone = policy.get("tone",{})
    phrase = policy.get("phrasebook",{})
    must_include = "・".join(policy.get("must_include",[]))
    avoid_words  = "・".join(policy.get("avoid_phrases",[]))

    # 価値観タグ
    vmapping = policy.get("value_mapping",{})
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

    # few-shot（簡略）と理論カタログ
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

    # Single-Shot MAX プロンプト
    prompt = f"""
あなたは教育支援AI「Albert」。先生を支え、子どもの成長と保護者の安心を後押しし、学校の価値観に合う提案だけを返します。
出力は「ねぎらい→具体策→言い換え（保護者/生徒）→観察→注意」。

【学校ポリシー（要反映）】
- 必須: {must_include}
- 避ける言い回し: {avoid_words}
- フレーズ集: 先生冒頭「{phrase.get('teacher_open','')}」/ 保護者冒頭「{phrase.get('parent_open','')}」
- トーン: 先生={tone.get('teacher','')} / 保護者={tone.get('parent','')} / 低学年={tone.get('student_low','')} / 中高生={tone.get('student_high','')}

{personalization}
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

    # 生成
    with st.spinner("生成中..."):
        res = client.chat.completions.create(
            model=MODEL,
            messages=[{"role":"system","content": fewshot + "\n" + prompt}],
            temperature=0.45, max_tokens=1200
        )
        text = res.choices[0].message.content

        # 安全NGが含まれていれば再生成
        if needs_safety and violates_ng(text):
            fix = "【修正指示】安全最優先・分離と見守り・記録と報告を前提に、被害側の曝露を避け、個別/環境調整中心で再提案。"
            res2 = client.chat.completions.create(
                model=MODEL,
                messages=[{"role":"system","content": fewshot + "\n" + prompt + "\n" + fix}],
                temperature=0.4, max_tokens=1200
            )
            text = res2.choices[0].message.content

    # スナップショット
    chips = [grade, scale, scene, frequency, urgency, emotion, subject, timebox] + values + (["安全モード"] if needs_safety else [])
    st.caption("この入力で生成：　" + " / ".join([c for c in chips if c]))

    # タイプライタ表示
    holder = st.empty()
    typewriter(holder, text, step=0.02)

    # 相談ログ（匿名）を保存
    events = load_json(EVENTS_PATH, {"events":[]})
    sid = st.session_state.get("sid") or uuid.uuid4().hex
    st.session_state["sid"] = sid
    topic_rules = [
        (r"いじめ|暴力|脅|自傷|自殺|安全|被害", "安全/人間関係"),
        (r"保護者|家庭|連絡|面談", "保護者対応"),
        (r"提出|宿題|課題|忘れ|未提出", "課題・提出"),
        (r"遅刻|欠席|不登校|登校しぶり", "出欠"),
        (r"集中|立ち歩き|私語|規律|荒れ", "授業規律"),
        (r"評価|テスト|成績|アセスメント", "評価"),
        (r"友だち|仲間|グループ|孤立", "関係性"),
    ]
    tags=set()
    for pat, tag in topic_rules:
        if re.search(pat, message): tags.add(tag)
    if not tags: tags={"未分類"}
    events["events"].append({
        "ts": time.time(),
        "user": sid,
        "grade": grade, "scene": scene, "urgency": urgency, "emotion": emotion,
        "subject": subject, "timebox": timebox, "values": values,
        "sensitive": needs_safety, "topics": sorted(list(tags))
    })
    events["events"] = events["events"][-500:]
    save_json(EVENTS_PATH, events)

    # フィードバック保存＆再生成（追記）
    with st.expander("しっくりきませんか？ フィードバック / 追記して再生成"):
        c1, c2 = st.columns([1,2])
        rating = c1.radio("役立ち度", ["good","ok","bad"], horizontal=True, index=1)
        reasons = c2.multiselect("不足していた点（複数可）", REASONS)
        note = st.text_input("メモ（任意）", placeholder="例）保護者への伝え方を厚めに")
        refine = st.text_input("この追記で再生成（任意）", placeholder="例）声かけ例を3つ、所要時間は10分以内に")
        b1, b2 = st.columns(2)
        if b1.button("フィードバックを保存"):
            mem = load_json(MEM_PATH, {"users":{}})
            u = mem["users"].setdefault(uid, {"feedback":[]})
            u["feedback"].append({
                "res_id": str(uuid.uuid4().hex),
                "rating": rating, "reasons": reasons, "note": note,
                "input": {
                    "grade": grade, "scale": scale, "scene": scene, "frequency": frequency,
                    "urgency": urgency, "emotion": emotion, "subject": subject, "timebox": timebox,
                    "specificity": specificity, "values": values
                }
            })
            u["feedback"] = u["feedback"][-200:]
            save_json(MEM_PATH, mem)
            st.success("保存しました。次回の提案に反映されます。")

        if b2.button("追記して再生成") and refine.strip():
            st.session_state["refine_override"] = refine.strip()
            st.rerun()

# 追記があれば自動再生成（簡易）
if ro := st.session_state.get("refine_override"):
    st.session_state["refine_override"] = ""
    st.info(f"追加指定：{ro}")

    # 直近入力がフォームに残っている前提で、追加指定だけつけて再生成
    # （簡易版：プロンプト末尾に追記するだけ）
    # 値の取得
    grade = st.session_state.get("albert_form-grade", "") or st.session_state.get("grade", "")
    # 以降は上の生成処理を関数化して呼ぶのが理想だが、βでは一旦ここで終了
    st.warning("再生成は上の『提案を生成』をもう一度押してください（追加指定は反映されます）。")

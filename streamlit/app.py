import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
import streamlit as st
import pandas as pd
from google import genai

# ==========================================
# 1. ページ基本設定
# ==========================================
st.set_page_config(
    page_title="AI無音カット＆テロップメーカー",
    page_icon="🎬",
    layout="centered"
)

# スマホ向けレイアウト調整CSS
st.markdown("""
<style>
    .stButton>button { width: 100%; border-radius: 8px; height: 3em; font-weight: bold; }
    div[data-testid="stStatusWidget"] { border-radius: 8px; }
</style>
""", unsafe_allow_html=True)


# ==========================================
# 2. パスワード認証機能
# ==========================================
CORRECT_PASSWORD = st.secrets.get("APP_PASSWORD", "secret1234")

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

def check_password():
    st.title("🔒 ログイン認証")
    st.caption("このアプリを利用するにはパスワードを入力してください。")

    pwd_input = st.text_input("パスワード", type="password", key="login_password_input")
    if st.button("ログイン", type="primary"):
        if pwd_input == CORRECT_PASSWORD:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("❌ パスワードが正しくありません。")

if not st.session_state.authenticated:
    check_password()
    st.stop()


# ==========================================
# 3. 認証後：メイン画面
# ==========================================
st.sidebar.success("✅ ログイン済み")
if st.sidebar.button("ログアウト"):
    st.session_state.authenticated = False
    st.session_state.subtitles_data = None
    st.session_state.processed_video_bytes = None
    st.session_state.video_file_name = None
    st.rerun()

st.title("🎬 AI無音カット ＆ テロップメーカー")
st.caption("無音自動カット ➜ AI文字起こし ➜ テロップ修正・装飾 ➜ 動画出力")

# APIキーの取得
api_key = st.secrets.get("GEMINI_API_KEY", None)
if not api_key:
    api_key = st.sidebar.text_input("Gemini API Key を入力", type="password")

if not api_key:
    st.info("💡 画面左上のサイドバーから Gemini API Key を入力するか、Secretsに設定してください。")
    st.stop()

client = genai.Client(api_key=api_key)

# セッション状態の初期化
if "subtitles_data" not in st.session_state:
    st.session_state.subtitles_data = None
if "processed_video_bytes" not in st.session_state:
    st.session_state.processed_video_bytes = None
if "video_file_name" not in st.session_state:
    st.session_state.video_file_name = None


# ==========================================
# 4. FFmpeg 無音カット処理関数
# ==========================================
def cut_silence(input_path: Path, output_path: Path, noise_db: int = -30, min_duration: float = 0.5):
    """FFmpegのsilencedetectを使って無音区間を検出し、有音部分だけを結合する"""
    cmd = [
        "ffmpeg", "-i", str(input_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
        "-f", "null", "-"
    ]
    res = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)

    silence_starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", res.stderr)]
    silence_ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", res.stderr)]

    dur_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(input_path)]
    total_dur = float(subprocess.run(dur_cmd, stdout=subprocess.PIPE, text=True).stdout.strip())

    keep_segments = []
    current_time = 0.0

    for start, end in zip(silence_starts, silence_ends):
        if start > current_time:
            keep_segments.append((current_time, start))
        current_time = end
    if current_time < total_dur:
        keep_segments.append((current_time, total_dur))

    if not keep_segments:
        subprocess.run(["ffmpeg", "-y", "-i", str(input_path), "-c", "copy", str(output_path)], check=True)
        return

    filter_parts = []
    concat_inputs = []
    for i, (seg_start, seg_end) in enumerate(keep_segments):
        filter_parts.append(f"[0:v]trim=start={seg_start}:end={seg_end},setpts=PTS-STARTPTS[v{i}];")
        filter_parts.append(f"[0:a]atrim=start={seg_start}:end={seg_end},asetpts=PTS-STARTPTS[a{i}];")
        concat_inputs.append(f"[v{i}][a{i}]")

    filter_complex = "".join(filter_parts) + f"{''.join(concat_inputs)}concat=n={len(keep_segments)}:v=1:a=1[outv][outa]"

    concat_cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        str(output_path)
    ]
    subprocess.run(concat_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


# ==========================================
# 5. メイン画面操作フロー
# ==========================================
uploaded_file = st.file_uploader("動画ファイルを選択", type=["mp4", "mov", "m4v", "mkv"])

if uploaded_file is not None:
    if st.session_state.video_file_name != uploaded_file.name:
        st.session_state.video_file_name = uploaded_file.name
        st.session_state.processed_video_bytes = None
        st.session_state.subtitles_data = None

    st.subheader("⚙️ 前処理オプション")
    enable_silence_cut = st.checkbox("✂️ 無音部分を自動カット（ジャンプカット）する", value=True)
    if enable_silence_cut:
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            silence_dur = st.slider("無音とみなす秒数（秒）", 0.3, 1.5, 0.5, 0.1, help="この秒数以上無音が続くとカットされます")
        with col_s2:
            silence_db = st.slider("無音感度（dB）", -45, -20, -30, 5, help="低いほど小さな音でも無音と判定します")

    # Step 1: 解析ボタン
    if st.session_state.subtitles_data is None:
        if st.button("🚀 動画を処理してテロップを自動生成", type="primary"):
            status_box = st.status("動画の解析とテロップ生成を開始します...", expanded=True)

            with tempfile.TemporaryDirectory() as temp_dir:
                temp_dir = Path(temp_dir)
                raw_video = temp_dir / "raw_input.mp4"
                cut_video = temp_dir / "cut_video.mp4"
                temp_audio = temp_dir / "audio.mp3"

                with open(raw_video, "wb") as f:
                    f.write(uploaded_file.getvalue())

                # 1. 無音カット
                if enable_silence_cut:
                    status_box.update(label="1/3: 無音区間を検出してカット中...")
                    cut_silence(raw_video, cut_video, noise_db=silence_db, min_duration=silence_dur)
                    working_video = cut_video
                else:
                    working_video = raw_video

                with open(working_video, "rb") as f:
                    st.session_state.processed_video_bytes = f.read()

                # 2. 音声抽出
                status_box.update(label="2/3: 音声を抽出中...")
                subprocess.run([
                    "ffmpeg", "-y", "-i", str(working_video),
                    "-vn", "-acodec", "libmp3lame", "-b:a", "128k", str(temp_audio)
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

                # 3. Gemini文字起こし
                status_box.update(label="3/3: Gemini AIがテロップを生成中...")
                audio_file = client.files.upload(file=str(temp_audio))
                while audio_file.state.name == "PROCESSING":
                    time.sleep(2)
                    audio_file = client.files.get(name=audio_file.name)

                prompt = """
動画音声を書き起こし、正確なタイムスタンプ付きの標準的なSRTフォーマットのみを出力してください。
【厳格な要件】
1. Markdown記号（```srt 等）や挨拶文は一切含めない。
2. 1セクションあたり12〜16文字程度でテンポよく分割する。
3. フィラー（えーと、あの等）は削除し、句読点は半角スペースに置き換える。
"""
                res = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[audio_file, prompt]
                )
                client.files.delete(name=audio_file.name)

                # SRTをパース
                raw_srt = re.sub(r"^
http://googleusercontent.com/immersive_entry_chip/0

---

### 【ステップ3】Streamlit CloudのSecretsに貼り付ける内容

> Streamlit Cloudの **「Advanced settings...」➜「Secrets」** 枠に貼り付けます。  
> `""` の中をご自身のパスワードとAPIキーに書き換えてください。

```toml
# アプリを開く際に入力するパスワード（半角英数字で自由に設定）
APP_PASSWORD = "your_password_here"

# Google AI Studioで取得したAPIキー（AIzaSy...）
GEMINI_API_KEY = "your_gemini_api_key_here"

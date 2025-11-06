import os, io, time, zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
import pandas as pd
import streamlit as st

STRAVA_AUTH = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN = "https://www.strava.com/oauth/token"
STRAVA_API   = "https://www.strava.com/api/v3"

CLIENT_ID     = st.secrets.get("STRAVA_CLIENT_ID", "")
CLIENT_SECRET = st.secrets.get("STRAVA_CLIENT_SECRET", "")
DEFAULT_REDIRECT = "http://localhost:8501"  # used for local tests

def exchange_code_for_token(code: str):
    r = requests.post(STRAVA_TOKEN, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=30)
    r.raise_for_status()
    return r.json()

def refresh_token(refresh_token: str):
    r = requests.post(STRAVA_TOKEN, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, timeout=30)
    r.raise_for_status()
    return r.json()

def bearer(tok): return {"Authorization": f"Bearer {tok}"}

def get_json(url, tok, params=None):
    resp = requests.get(url, headers=bearer(tok), params=params or {}, timeout=60)
    if resp.status_code == 401:
        raise RuntimeError("expired")
    resp.raise_for_status()
    return resp.json()

def fetch_last_n_days(tok, days=7):
    after = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    page, all_items = 1, []
    while True:
        arr = get_json(f"{STRAVA_API}/athlete/activities", tok, {"per_page":100, "page":page, "after":after})
        if not arr: break
        all_items.extend(arr)
        if len(arr) < 100: break
        page += 1
    return all_items

def flatten_activity(a):
    return {
        "id": a.get("id"),
        "name": a.get("name"),
        "type": a.get("type"),
        "sport_type": a.get("sport_type"),
        "start_date": a.get("start_date"),
        "start_date_local": a.get("start_date_local"),
        "elapsed_time_sec": a.get("elapsed_time"),
        "moving_time_sec": a.get("moving_time"),
        "distance_m": a.get("distance"),
        "total_elevation_gain_m": a.get("total_elevation_gain"),
        "average_speed_mps": a.get("average_speed"),
        "max_speed_mps": a.get("max_speed"),
        "average_cadence": a.get("average_cadence"),
        "average_heartrate": a.get("average_heartrate"),
        "max_heartrate": a.get("max_heartrate"),
        "weighted_average_watts": a.get("weighted_average_watts"),
        "average_watts": a.get("average_watts"),
        "device_watts": a.get("device_watts"),
        "average_temp": a.get("average_temp"),
        "elev_high_m": a.get("elev_high"),
        "elev_low_m": a.get("elev_low"),
        "pr_count": a.get("pr_count"),
        "kudos_count": a.get("kudos_count"),
        "comment_count": a.get("comment_count"),
        "achievement_count": a.get("achievement_count"),
        "gear_id": a.get("gear_id"),
        "distance_miles": (a.get("distance") or 0) * 0.000621371,
        "elev_gain_ft": (a.get("total_elevation_gain") or 0) * 3.28084,
        "avg_pace_min_per_mile": (26.8224 / a.get("average_speed")) if a.get("average_speed") else None,
        "avg_speed_mph": (a.get("average_speed") or 0) * 2.237,
        "max_speed_mph": (a.get("max_speed") or 0) * 2.237,
    }

def weekly_markdown(df: pd.DataFrame) -> str:
    if df.empty: return "# Weekly Training Summary\n\nNo activities in the last 7 days."
    df["date"] = pd.to_datetime(df["start_date_local"]).dt.date
    total_time = int(df["moving_time_sec"].fillna(0).sum())
    h, rem = divmod(total_time, 3600); m = rem // 60
    total_mi = round(df["distance_miles"].fillna(0).sum(), 1)
    by_sport = df.groupby("sport_type").agg(sessions=("id","count"),
                                            time_sec=("moving_time_sec","sum"),
                                            miles=("distance_miles","sum"),
                                            avg_hr=("average_heartrate","mean")).reset_index()
    lines = ["# Weekly Training Summary (last 7 days)", ""]
    lines.append(f"**Total**: {total_mi} miles across **{h}h {m}m**"); lines.append("")
    for _, r in by_sport.iterrows():
        th, tr = divmod(int(r.time_sec), 3600); tm = tr // 60
        if pd.notna(r.avg_hr):
            lines.append(f"- **{r.sport_type}**: {int(r.sessions)} sessions, {th}h {tm}m, {r.miles:.1f} mi, avg HR {r.avg_hr:.0f}")
        else:
            lines.append(f"- **{r.sport_type}**: {int(r.sessions)} sessions, {th}h {tm}m, {r.miles:.1f} mi")
    lines.append("")
    longs = []
    for sp in by_sport["sport_type"]:
        sub = df[df["sport_type"] == sp].sort_values("distance_miles", ascending=False)
        if not sub.empty:
            r = sub.iloc[0]
            longs.append(f"- {sp}: {r['distance_miles']:.1f} mi on {r['date']}")
    if longs:
        lines.append("**Longest Sessions**"); lines.extend(longs); lines.append("")
    lines.append("> Paste this in ChatGPT and ask: “Review this report and propose my plan for next week (Ironman focus).”")
    return "\n".join(lines)

def zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            z.writestr(path, content)
    buf.seek(0)
    return buf.read()

st.set_page_config(page_title="PaceIQ Exporter", page_icon="🏃‍♂️", layout="centered")
st.title("PaceIQ — Strava Weekly Export")

if not CLIENT_ID or not CLIENT_SECRET:
    st.error("Missing STRAVA_CLIENT_ID or STRAVA_CLIENT_SECRET in Streamlit secrets.")
    st.stop()

if "token" not in st.session_state: st.session_state.token = None
if "athlete" not in st.session_state: st.session_state.athlete = None

code = st.query_params.get("code")
if code and not st.session_state.token:
    with st.spinner("Authorizing with Strava..."):
        tok = exchange_code_for_token(code)
        st.session_state.token = {
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token"),
            "expires_at": tok.get("expires_at"),
        }
        st.session_state.athlete = tok.get("athlete", {})
        st.success("Connected to Strava")
        st.query_params.clear()

if not st.session_state.token:
    st.subheader("Step 1 — Connect your Strava")
    scope = "read,activity:read_all"
    # On Streamlit Cloud, your app URL is the redirect. If running locally, it is DEFAULT_REDIRECT.
    redirect_uri = os.environ.get("STREAMLIT_SERVER_URL", DEFAULT_REDIRECT)
    auth_url = f"{STRAVA_AUTH}?{urlencode({'client_id':CLIENT_ID,'response_type':'code','redirect_uri':redirect_uri,'scope':scope,'approval_prompt':'auto'})}"
    st.link_button("Connect with Strava", auth_url, type="primary")
    st.caption("After authorizing, you will land back here automatically.")
    st.stop()

# Token refresh
if int(time.time()) >= int(st.session_state.token.get("expires_at") or 0):
    with st.spinner("Refreshing token..."):
        newtok = refresh_token(st.session_state.token["refresh_token"])
        st.session_state.token.update({
            "access_token": newtok["access_token"],
            "refresh_token": newtok.get("refresh_token", st.session_state.token["refresh_token"]),
            "expires_at": newtok.get("expires_at"),
        })

st.success(f"Connected as {st.session_state.athlete.get('firstname','')} {st.session_state.athlete.get('lastname','')}")

st.subheader("Step 2 — Fetch activities")
days = st.slider("Days to include", 7, 28, 7)
if st.button("Fetch Activities", type="primary"):
    try:
        with st.spinner("Pulling activities..."):
            acts = fetch_last_n_days(st.session_state.token["access_token"], days)
            detailed_rows, lap_rows = [], []
            for a in acts:
                det = get_json(f"{STRAVA_API}/activities/{a['id']}", st.session_state.token["access_token"], {"include_all_efforts":"true"})
                detailed_rows.append(flatten_activity(det))
                try:
                    laps = get_json(f"{STRAVA_API}/activities/{a['id']}/laps", st.session_state.token["access_token"])
                    for L in laps:
                        lap_rows.append({
                            "activity_id": a["id"],
                            "lap_index": L.get("lap_index"),
                            "name": L.get("name"),
                            "elapsed_time_sec": L.get("elapsed_time"),
                            "moving_time_sec": L.get("moving_time"),
                            "distance_m": L.get("distance"),
                            "avg_speed_mps": L.get("average_speed"),
                            "max_speed_mps": L.get("max_speed"),
                            "avg_heartrate": L.get("average_heartrate"),
                            "max_heartrate": L.get("max_heartrate"),
                            "elev_gain_m": L.get("total_elevation_gain"),
                            "elev_loss_m": L.get("total_elevation_loss"),
                            "split": L.get("split"),
                        })
                except Exception:
                    pass

            df = pd.DataFrame(detailed_rows)
            laps_df = pd.DataFrame(lap_rows)

            st.write("### Activities")
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.write("### Laps")
            st.dataframe(laps_df, use_container_width=True, hide_index=True)

            md = weekly_markdown(df)
            st.write("### Weekly Report")
            st.code(md, language="markdown")

            files = {
                "activities_detailed.csv": df.to_csv(index=False).encode("utf-8"),
                "laps.csv": laps_df.to_csv(index=False).encode("utf-8"),
                "weekly_report.md": md.encode("utf-8"),
            }
            today = datetime.now().strftime("%Y-%m-%d")
            zip_data = zip_bytes({f"{today}/"+k: v for k, v in files.items()})

            st.download_button("⬇️ Download Weekly ZIP", data=zip_data, file_name=f"PaceIQ_{today}.zip", mime="application/zip")
            st.download_button("⬇️ Download activities_detailed.csv", data=files["activities_detailed.csv"], file_name="activities_detailed.csv", mime="text/csv")
            st.download_button("⬇️ Download laps.csv", data=files["laps.csv"], file_name="laps.csv", mime="text/csv")
            st.download_button("⬇️ Download weekly_report.md", data=files["weekly_report.md"], file_name="weekly_report.md", mime="text/markdown")
            st.success("Ready. Upload the ZIP or CSVs to ChatGPT for analysis.")
    except Exception as e:
        st.error(f"Error: {e}")

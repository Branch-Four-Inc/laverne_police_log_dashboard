import pandas as pd
import plotly.express as px
import pydeck as pdk
import streamlit as st

st.set_page_config(page_title="Police Incidents", layout="wide")

DOW_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SAFE_COLORS = px.colors.qualitative.Safe
# 12 a.m., 1 a.m., ... 11 p.m. — used to force chronological (not alphabetical) order on the heatmap x-axis
HOUR_LABELS = [f"{h % 12 or 12} {'a.m.' if h < 12 else 'p.m.'}" for h in range(24)]


def _to_rgb(c):
    if c.startswith("#"):
        c = c.lstrip("#")
        return [int(c[i : i + 2], 16) for i in (0, 2, 4)]
    return [int(x) for x in c[4:-1].split(",")]


@st.cache_data(ttl=3600)
def load():
    try:
        df = pd.read_csv("daily_logs_geocoded.csv")
    except FileNotFoundError:
        df = pd.read_csv("daily_logs.csv")
    df["reported"] = pd.to_datetime(df["reported"])
    df["log_date"] = pd.to_datetime(df["log_date"], format="%m.%d.%Y")
    df["hour"] = df["reported"].dt.hour
    df["dow"] = df["reported"].dt.day_name()
    natures = sorted(df["grouped_nature"].dropna().unique())
    color_map = {n: _to_rgb(SAFE_COLORS[i % len(SAFE_COLORS)]) for i, n in enumerate(natures)}
    return df, natures, color_map


df, natures, color_map = load()
min_date, max_date = df["log_date"].min().date(), df["log_date"].max().date()

# ── Sidebar ────────────────────────────────────────────────────────────────────
st.sidebar.header("Filters")
date_range = st.sidebar.date_input("Date range", (min_date, max_date), min_value=min_date, max_value=max_date)

# Multiselect has a built-in "clear all" (the x) but nothing to get back to everything
# selected -- this button sets session_state before the widget below reads it.
if st.sidebar.button("Select All"):
    st.session_state["incident_type_filter"] = natures
selected = st.sidebar.multiselect("Incident Type", natures, default=natures, key="incident_type_filter")

address_query = st.sidebar.text_input("Street address contains", placeholder="e.g. Foothill Blvd")

start, end = date_range if len(date_range) == 2 else (min_date, max_date)
fdf = df[df["log_date"].dt.date.between(start, end) & df["grouped_nature"].isin(selected)]

# Simple substring match so partial street names work (e.g. "foothill" matches "2269 FOOTHILL BLVD")
if address_query:
    fdf = fdf[fdf["incident_address"].str.contains(address_query, case=False, na=False)]

st.title("La Verne Police Incidents")
count_col1, count_col2 = st.columns(2)
count_col1.metric("Filtered Incidents", f"{len(fdf):,}")
count_col2.metric("Total Incidents (all dates & natures)", f"{len(df):,}")

# ── Map ────────────────────────────────────────────────────────────────────────
if "lat" in fdf.columns and fdf["lat"].notna().any():
    map_df = fdf.dropna(subset=["lat", "lon"]).copy()
    map_df["color"] = map_df["grouped_nature"].map(color_map)
    st.subheader("Incident Map")
    st.pydeck_chart(
        pdk.Deck(
            initial_view_state=pdk.ViewState(latitude=map_df["lat"].mean(), longitude=map_df["lon"].mean(), zoom=13),
            layers=[
                pdk.Layer(
                    "ScatterplotLayer",
                    data=map_df,
                    get_position=["lon", "lat"],
                    get_fill_color="color",
                    get_radius=40,
                    pickable=True,
                )
            ],
            tooltip={"text": "{grouped_nature}\n{incident_address}"},
            map_style="light",
        )
    )
else:
    st.info("Map unavailable — run `geocode_neighborhoods.py` then `geocode_census_fallback.py` to add coordinates.")

# ── Heatmap ────────────────────────────────────────────────────────────────────
st.subheader("Total Incidents by Day of Week & Hour")
heat = fdf.groupby(["dow", "hour"]).size().reset_index(name="count")
heat["dow"] = pd.Categorical(heat["dow"], categories=DOW_ORDER, ordered=True)

# Raw hour is 0-23, which isn't intuitive to read — swap in "12 AM", "1 AM", etc.
heat["hour_label"] = heat["hour"].map(lambda h: HOUR_LABELS[h])

fig_heat = px.density_heatmap(
    heat.sort_values("dow"),
    x="hour_label",
    y="dow",
    z="count",
    color_continuous_scale="Blues",
    category_orders={"hour_label": HOUR_LABELS, "dow": DOW_ORDER},
    labels={"hour_label": "Hour of Day", "dow": "", "count": "Incidents"},
)
fig_heat.update_layout(height=320, margin=dict(t=10, b=10))
st.plotly_chart(fig_heat, use_container_width=True)

# ── Daily chart ────────────────────────────────────────────────────────────────
st.subheader("Total Incidents by Day")
if st.toggle("Incident Type breakdown"):
    daily = fdf.groupby(["log_date", "grouped_nature"]).size().reset_index(name="count")
    fig = px.bar(
        daily,
        x="log_date",
        y="count",
        color="grouped_nature",
        color_discrete_sequence=SAFE_COLORS,
        labels={"log_date": "Date", "count": "Incidents", "grouped_nature": "Incident Type"},
    )
else:
    daily = fdf.groupby("log_date").size().reset_index(name="count")
    daily["rolling_7"] = daily["count"].rolling(7, min_periods=1).mean()
    fig = px.line(daily, x="log_date", y="count", labels={"log_date": "Date", "count": "Incidents"})
    fig.update_traces(line=dict(color="#c8d8e8", width=1), name="Daily")
    fig.add_scatter(x=daily["log_date"], y=daily["rolling_7"], name="7-day avg", line=dict(color="#1a4a8a", width=2.5))

fig.update_layout(height=350, margin=dict(t=10, b=10))
fig.update_xaxes(tickformat="%B %Y")  # full month name (e.g. "April 2025") instead of the "Apr 2025" default
st.plotly_chart(fig, use_container_width=True)

# ── Assist Agency table ───────────────────────────────────────────────────────
# Pulled out separately since these get folded into "Welfare/Assist" above and are
# easy to miss. Also a lot of these are calls where LVPD helped a *different* agency
# at an address in another city (not La Verne) — worth reviewing on their own rather
# than plotted on the map, since we can't assume they're actually here.
st.subheader("Assist Agency Calls")
st.caption(
    "Raw \"ASSIST AGENCY\" incidents, shown separately from the Incident Type filter above. "
    "Some of these are mutual-aid calls to other cities, not La Verne itself — addresses "
    "without lat/lon below are excluded from the map and OpenAI backfill for that reason."
)

assist = df[df["log_date"].dt.date.between(start, end) & (df["nature"] == "ASSIST AGENCY")]
if address_query:
    assist = assist[assist["incident_address"].str.contains(address_query, case=False, na=False)]

st.dataframe(
    assist[["reported", "incident_address", "lat", "lon"]].sort_values("reported", ascending=False),
    use_container_width=True,
    hide_index=True,
)
st.caption(f"{len(assist):,} Assist Agency calls in the selected date range ({assist['lat'].notna().sum():,} geocoded)")

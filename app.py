import pandas as pd
import plotly.express as px
import pydeck as pdk
import streamlit as st
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta


st.set_page_config(page_title="La Verne Police Log Dashboard", layout="wide")

DOW_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
# 33 incident types, so a single plotly palette (Safe has 11) would repeat colors.
# Dark24 + Light24 has 48 colors; we use the first N_UNIQUE_COLORS of them.
N_UNIQUE_COLORS = 35  # number of distinct category colors (colors only repeat past this many incident types)
CATEGORY_COLORS = (px.colors.qualitative.Dark24 + px.colors.qualitative.Light24)[:N_UNIQUE_COLORS]
# 12 a.m., 1 a.m., ... 11 p.m. — used to force chronological (not alphabetical) order on the heatmap x-axis
HOUR_LABELS = [f"{h % 12 or 12} {'a.m.' if h < 12 else 'p.m.'}" for h in range(24)]


def _to_rgb(c):
    if c.startswith("#"):
        c = c.lstrip("#")
        return [int(c[i : i + 2], 16) for i in (0, 2, 4)]
    return [int(x) for x in c[4:-1].split(",")]


def legend_html(names):
    """Compact wrapping legend (colored dot + label) using the same colors as the map."""
    items = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;white-space:nowrap;">'
        f'<span style="width:10px;height:10px;border-radius:50%;flex:none;'
        f'background:rgb({color_map[n][0]},{color_map[n][1]},{color_map[n][2]});"></span>{n}</span>'
        for n in names
    )
    return f'<div style="display:flex;flex-wrap:wrap;gap:6px 18px;font-size:0.75rem;">{items}</div>'


# cache_resource (not cache_data) returns the same DataFrame each rerun instead of a fresh copy.
# Safe because the app only slices `df`, never mutates it.
@st.cache_resource(ttl=3600)
def load():
    try:
        df = pd.read_csv("police_logs_dashboard_work/output/daily_logs_geocoded.csv")
    except FileNotFoundError:
        df = pd.read_csv("police_logs_dashboard_work/output/daily_logs.csv")
    df["reported"] = pd.to_datetime(df["reported"])
    # Some source files are named with a 2-digit year (e.g. "08.18.26"), so try 4-digit first, then 2-digit
    log_date = pd.to_datetime(df["log_date"], format="%m.%d.%Y", errors="coerce")
    df["log_date"] = log_date.fillna(pd.to_datetime(df["log_date"], format="%m.%d.%y", errors="coerce"))
    df["hour"] = df["reported"].dt.hour
    df["dow"] = df["reported"].dt.day_name()
    natures = sorted(df["grouped_nature"].dropna().unique())
    color_map = {n: _to_rgb(CATEGORY_COLORS[i % len(CATEGORY_COLORS)]) for i, n in enumerate(natures)}

    # Precompute per-row values once here (this function is cached) instead of on every rerun
    df["log_day"] = df["log_date"].dt.normalize()  # datetime64 so date filters are vectorized
    df["color"] = df["grouped_nature"].map(color_map)
    df["address_lc"] = df["incident_address"].fillna("").str.lower()
    df["is_assist"] = df["nature"].str.strip().str.lower() == "assist agency"
    df["grouped_nature"] = df["grouped_nature"].astype("category")
    df["dow"] = df["dow"].astype("category")
    return df, natures, color_map


@st.cache_data
def load_data_dictionary():
    # Cached: reading the Excel file took ~0.4s on every interaction
    data_dict = pd.read_excel("nature_descriptions.xlsx", index_col=0)
    data_dict["Description"] = data_dict["Description"].fillna(data_dict["Incident Type"])
    return data_dict[["Incident Type", "Code", "Description"]].sort_values("Incident Type", ascending=True)


df, natures, color_map = load()
min_date, max_date = df["log_date"].min().date(), df["log_date"].max().date()

# ── Sidebar ────────────────────────────────────────────────────────────────────
# Defaults live in session_state so the "Reset all filters" button (main area) can restore them.
if "f_dates" not in st.session_state:
    st.session_state["f_dates"] = (min_date, max_date)
if "f_address" not in st.session_state:
    st.session_state["f_address"] = ""
if "incident_type_filter" not in st.session_state:
    st.session_state["incident_type_filter"] = list(natures)


# Buttons use on_click callbacks, which run before the widgets below are built, so they can
# safely set the widgets' session_state values.
def _select_all_types():
    st.session_state["incident_type_filter"] = list(natures)


def _clear_all_types():
    st.session_state["incident_type_filter"] = []


def _reset_all_filters():
    st.session_state["f_dates"] = (min_date, max_date)
    st.session_state["f_address"] = ""
    _select_all_types()


# Make the Select all / Clear all buttons smaller (they're the only buttons in the sidebar)
st.sidebar.markdown(
    """<style>
    section[data-testid="stSidebar"] .stButton button {
        min-height: 1.6rem; padding: 0.1rem 0.5rem; font-size: 0.75rem;
    }
    </style>""",
    unsafe_allow_html=True,
)

st.sidebar.header("Filters")
st.sidebar.caption("Use the filters below to narrow the incidents you want to see. You can filter by Date, Street Address or Incident Type.")

# Widget labels render smaller than markdown text, so use a markdown label (same as "Incident Type")
# and hide the built-in one.
st.sidebar.markdown("**Date Range**")
date_range = st.sidebar.date_input(
    "Date Range", min_value=min_date, max_value=max_date, key="f_dates", format="MM/DD/YYYY",
    label_visibility="collapsed"
)

st.sidebar.markdown("**Street Address Contains**")
address_query = st.sidebar.text_input(
    "Street Address Contains", placeholder="e.g. Foothill Blvd", key="f_address", label_visibility="collapsed"
)

st.sidebar.markdown("**Incident Type**")
btn_all, btn_none = st.sidebar.columns(2)
btn_none.button("Clear all", on_click=_clear_all_types, width="stretch")
btn_all.button("Select all", on_click=_select_all_types, width="stretch")
selected = st.sidebar.multiselect(
    "Incident Type",
    natures,
    key="incident_type_filter",
    label_visibility="collapsed",
    placeholder="Choose one or more incident types",
)
if not selected:
    st.sidebar.caption("No incident types selected, so no incidents are shown.")

st.sidebar.markdown("---")
st.sidebar.caption("Created by Branch Four Inc.")

start, end = date_range if len(date_range) == 2 else (min_date, max_date)
start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
fdf = df[df["log_day"].between(start_ts, end_ts) & df["grouped_nature"].isin(selected)]


# Simple substring match so partial street names work (e.g. "foothill" matches "2269 FOOTHILL BLVD")
if address_query:
    fdf = fdf[fdf["address_lc"].str.contains(address_query.strip().lower(), regex=False)]

st.title("La Verne Police Log Dashboard")
fmt_date = lambda d: f"{d:%b} {d.day}, {d:%Y}"  # "Jan 3, 2026" (no zero-padded day, works on Windows too)
st.subheader(f"{fmt_date(start)} – {fmt_date(end)}")
st.caption("Explore incidents reported to the La Verne Police Department. Use the filters in the left sidebar to narrow what you see.")

# ── Incidents summary ─────────────────────────────────────────────────────────
# Built from the filtered dataset (`fdf`), so it updates with the sidebar filters.
with st.container(border=True):
    #fmt_date = lambda d: f"{d:%b} {d.day}, {d:%Y}"  # "Jan 3, 2026" (no zero-padded day, works on Windows too)
    st.subheader("Summary")
    #st.caption("Reflects the current filters.")

    sum_cols = st.columns(3)
    sum_cols[0].metric(f"Filtered Incidents from {len(df):,} Total", f"{len(fdf):,}")
    if len(fdf):
        n_days = df["log_day"].nunique()  # every day in the data, not just days with filtered incidents
        # The single busiest day-of-week + hour combination (the darkest cell in the heatmap below),
        # not the busiest day and the busiest hour picked separately
        busiest_dow, busiest_hour_num = fdf.groupby(["dow", "hour"], observed=True).size().idxmax()
        busiest_hour = HOUR_LABELS[int(busiest_hour_num)]
        sum_cols[1].metric("Average per Day", f"{len(fdf) / n_days:,.1f}")
        sum_cols[2].metric("Busiest Day & Hour", f"{busiest_dow}, {busiest_hour}")

        top3 = fdf["grouped_nature"].value_counts().head(3)
        st.markdown("**Top 3 Incident Types**")
        top_cols = st.columns(3)
        for i, (name, cnt) in enumerate(top3.items()):
            with top_cols[i]:
                with st.container(border=True):
                    #st.caption(f"#{i + 1}")
                    st.markdown(f"**{i + 1}. {name}**")
                    st.caption(f"{cnt:,} incidents · {cnt / len(fdf):.1%} of filtered")
    else:
        sum_cols[1].metric("Average per Day", "—")
        sum_cols[2].metric("Busiest Day & Hour", "—")
        st.markdown("**Top 3 Incident Types**")
        st.caption("—")

_, reset_col = st.columns([4, 1])
reset_col.button("Reset all filters", on_click=_reset_all_filters, width="stretch")

# With no matching incidents every chart would be empty, so show one message instead    
has_rows = len(fdf) > 0
if not has_rows:
    st.warning("No incidents match the current filters. Try widening the date range, selecting more incident types, or clearing the address search.")
else:
    # ── Map ────────────────────────────────────────────────────────────────────────
    if "lat" in fdf.columns and fdf["lat"].notna().any():
        # Only send the columns the map needs -- every column of every row gets serialized to the browser
        map_df = fdf.dropna(subset=["lat", "lon"])[["lat", "lon", "color", "date", "grouped_nature", "incident_address"]]
    
        st.subheader("Incident Map")
        st.caption("The Incident Map shows where reported incidents occurred. Hover over (or tap) an incident on the map to see more information. Pinch or use two fingers to zoom.")
        # pydeck has no built-in legend, so draw one with HTML: a colored dot per incident type
        # that is actually on the map right now (uses the same colors as the map layer).
        # Collapsed in an expander: with 30+ types it is very tall on a phone and would push the map off-screen.
        present_set = set(map_df["grouped_nature"].unique())
        present = [n for n in natures if n in present_set]
        with st.expander("Map Legend (incident type colors)"):
            st.markdown(legend_html(present), unsafe_allow_html=True)
        st.pydeck_chart(
            pdk.Deck(
                initial_view_state=pdk.ViewState(latitude=map_df["lat"].mean(), longitude=map_df["lon"].mean(), zoom=12),
                layers=[
                    pdk.Layer(
                        "ScatterplotLayer",
                        data=map_df,
                        get_position=["lon", "lat"],
                        get_fill_color="color",
                        get_radius=28,
                        radius_min_pixels=6,   # big enough to tap on a phone even when zoomed out
                        radius_max_pixels=14,
                        pickable=True,
                    )
                ],
                tooltip={
                    "html": "<b>{grouped_nature}</b><br/>{date}<br/>{incident_address}",
                    "style": {"fontSize": "14px", "maxWidth": "240px", "whiteSpace": "normal"},
                },
                map_style="light",
                # A flat, north-up map: no accidental rotate/tilt when pinching to zoom on a phone
                views=[pdk.View(type="MapView", controller={"dragRotate": False, "touchRotate": False})],
            ),
            width="stretch",  # fill the screen width on a phone
            height=420,       # leaves room on a phone to scroll past the map
        )
        st.caption(
            f"Note: Only incidents with full addresses are mapped. {len(fdf) - len(map_df):,} incidents are missing full addresses and are not mapped. "
            "Map locations are generated approximations and may not reflect the actual location of an incident. "
                  )
    else:
        st.info("Map unavailable — run `geocode_neighborhoods.py` then `geocode_census_fallback.py` to add coordinates.")


    # ── Daily chart ────────────────────────────────────────────────────────────────
    # A fragment, so toggling the breakdown reruns only this chart, not the whole page.
    # `fdf` is passed in as an argument so the fragment keeps its own copy of the filtered data.
    @st.fragment
    def daily_chart(fdf):
        st.subheader("Total Incidents by Day")
        st.caption('''The light grey line shows the total number of reported incidents by date. 
                   The dark blue line shows the 7-day average of the number of incidents. Click the 
                   'Incident Type Breakdown' to view the incident types by date.
                   ''')
        breakdown = st.toggle("Incident Type breakdown")
        if breakdown:
            daily = fdf.groupby(["log_date", "grouped_nature"], observed=True).size().reset_index(name="count")
            fig = px.bar(
                daily,
                x="log_date",
                y="count",
                color="grouped_nature",
                # same color per incident type as the map
                color_discrete_map={n: CATEGORY_COLORS[i % len(CATEGORY_COLORS)] for i, n in enumerate(natures)},
                category_orders={"grouped_nature": natures},
                labels={"log_date": "Date", "count": "Incidents", "grouped_nature": "Incident Type"},
            )
        else:
            daily = fdf.groupby("log_date").size().reset_index(name="count")
            daily["rolling_7"] = daily["count"].rolling(7, min_periods=1).mean()
            fig = px.line(daily, x="log_date", y="count", labels={"log_date": "Date", "count": "Incidents"})
            fig.update_traces(line=dict(color="#c8d8e8", width=1), name="Daily")
            fig.add_scatter(x=daily["log_date"], y=daily["rolling_7"], name="7-day Average", line=dict(color="#1a4a8a", width=2.5))

        fig.update_layout(height=350, margin=dict(t=10, b=10))
        if not breakdown:
            # one tooltip with both the daily count and the 7-day average. (Not used for the breakdown:
            # with 30+ stacked types a unified tooltip would be far too long.)
            fig.update_layout(hovermode="x unified")
        if breakdown:
            # Plotly can't lay a legend out in columns and scrolls (or stretches the chart) with 30+
            # entries, so hide it and show the compact wrapping legend above the chart instead.
            fig.update_layout(showlegend=False)
            # In an expander (like the map's legend): 30+ types is very tall on a phone and pushes the chart down
            with st.expander("Chart Legend (incident type colors)"):
                st.markdown(legend_html([n for n in natures if n in set(daily["grouped_nature"])]), unsafe_allow_html=True)
        # Tick labels adapt to the zoom level: days when the range is short, months when it's long.
        # (dtickrange is the tick spacing in milliseconds; "M1" etc. are month steps.)
        fig.update_xaxes(
            tickformatstops=[
                # short month names so labels don't overlap on a narrow screen
                dict(dtickrange=[None, 7 * 86400000], value="%b %d"),       # up to ~weekly ticks
                dict(dtickrange=[7 * 86400000, "M1"], value="%b %d"),       # weekly to monthly ticks
                dict(dtickrange=["M1", None], value="%b %Y"),               # monthly or wider
            ],
            hoverformat="%A, %B %d, %Y",  # hovering always shows the full date, e.g. "Monday, April 06, 2026"
            nticks=6,                     # fewer ticks, so they fit on a phone
            title=None,                   # the date axis is obvious; saves vertical space
        )
        # No zooming or panning on either axis (also lets a swipe over the chart scroll the page on touch screens)
        fig.update_xaxes(fixedrange=True)
        fig.update_yaxes(fixedrange=True)
        fig.update_layout(dragmode=False)
        # Legend (Daily / 7-day avg) in a row above the plot; the top margin makes room for it
        fig.update_layout(
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title_text=""),
            margin=dict(t=40, b=10, l=10, r=10),
        )
        # The toolbar (download, zoom buttons) is cramped on a phone, and zooming is turned off anyway
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

    daily_chart(fdf)

    # ── Heatmap ────────────────────────────────────────────────────────────────────
    st.subheader("Total Incidents by Day of Week & Hour")
    st.caption("The heatmap shows the total number of incidents by day of the week and time. This illustrates when incidents most often occur.")
    heat = fdf.groupby(["dow", "hour"], observed=True).size().reset_index(name="count")
    heat["dow"] = pd.Categorical(heat["dow"], categories=DOW_ORDER, ordered=True)

    # Raw hour is 0-23, which isn't intuitive to read — swap in "12 AM", "1 AM", etc.
    heat["hour_label"] = heat["hour"].map(lambda h: HOUR_LABELS[h])

    fig_heat = px.density_heatmap(
        heat.sort_values("dow"),
        x="hour_label",
        y="dow",
        z="count",
        color_continuous_scale="Blues",
        text_auto=False,  # print the count in each cell
        category_orders={"hour_label": HOUR_LABELS, "dow": DOW_ORDER},
        labels={"hour_label": "Hour of Day", "dow": "", "count": "Incidents"},
    )
    fig_heat.update_layout(
        height=320,
        margin=dict(t=50, b=10, l=10, r=10),  # top margin leaves room for the color bar legend
        dragmode=False,
        # Color bar legend as a short horizontal bar above the grid. Its numbers go above the bar too
        # ("outside top"), otherwise they would land on top of the grid.
        coloraxis_colorbar=dict(
            orientation="h",
            yref="paper", yanchor="bottom", y=1.0,
            xref="paper", xanchor="left", x=0,
            lenmode="pixels", len=280, thickness=10,
            ticklabelposition="outside top",
            title=dict(text="Total Incidents", side="right"),
        ),
    )
    # 24 hour labels don't fit on a phone: label every 4th hour (hovering a cell still shows its exact hour)
    fig_heat.update_xaxes(
        tickmode="array", tickvals=HOUR_LABELS[::4], ticktext=HOUR_LABELS[::4], tickangle=0,
        title=None, fixedrange=True,
    )
    fig_heat.update_yaxes(fixedrange=True)  # a 7 x 24 grid has nothing to zoom into; lets a swipe scroll the page on touch
    st.plotly_chart(fig_heat, width="stretch", config={"displayModeBar": False})



    # ── Table ────────────────────────────────────────────────────────────────
    st.subheader("Search Incidents")
    st.caption("The table lists the filtered incidents. Tap a column heading to sort, and scroll sideways to see every column.")

    table_df = fdf[['reported', 'grouped_nature', 'nature', 'incident_address']].rename({'reported': 'Report Date', 
                      'grouped_nature': 'Incident Type', 
                      'nature': 'Code',
                      'incident_address': 'Address'}, axis = 1)



    # Address before Description: on a narrow screen the first columns are all you see without swiping,
    # and the address is more useful than the long description
    table_df = table_df[['Report Date', 'Incident Type', 'Code', 'Address']].reset_index(drop = True)

    # The raw codes are lowercase ("disturb noise"): capitalize the first letter of every word.
    # str.title() would turn "don't" into "Don'T", so lowercase any letter right after an apostrophe.
    table_df["Code"] = (
        table_df["Code"].astype(str).str.upper()
    )

    st.dataframe(
        table_df,
        height=420,  # shorter than a phone screen, so a swipe outside the table can still scroll the page
        width="stretch",
        hide_index=True,
        column_config={
            # compact widths so more fits on screen before sideways scrolling
            "Report Date": st.column_config.DatetimeColumn("Report Date", format="MMM D, YYYY h:mm a", width="medium"),
            "Incident Type": st.column_config.TextColumn("Incident Type", width="medium"),
            "Code": st.column_config.TextColumn("Code", width="medium"),
            "Address": st.column_config.TextColumn("Address", width="medium"),
        },
    )


# ── Assist Agency dropdown ───────────────────────────────────────────────────────
# Pulled out separately since these get folded into "Welfare/Assist" above and are
# easy to miss. Also a lot of these are calls where LVPD helped a *different* agency
# at an address in another city (not La Verne) — worth reviewing on their own rather
# than plotted on the map, since we can't assume they're actually here.
#st.subheader("Assist Agency Calls")


assist = df[df["is_assist"] & df["log_day"].between(start_ts, end_ts)]

if address_query:
    assist = assist[assist["address_lc"].str.contains(address_query.strip().lower(), regex=False)]

assist = assist.rename({'reported': 'Report Date', 
                  'grouped_nature': 'Incident Type', 
                  'nature_description': 'Description', 
                  'incident_address': 'Address', 
                  'lat': 'Latitude', 
                  'lon': 'Longitude'}, axis = 1)

with st.expander("Assist Agency Calls"):
    st.caption('''
        The table lists all Assist Agency Calls. 'Assist Agency' are incidents where the La Verne police department 
        assists a nearby police department with an incident, or a nearby police department assists La Verne PD with an incident.
        Note: Raw \"ASSIST AGENCY\" incidents, are shown separately from the Incident Type filter.
        Addresses without lat/lon below are excluded from the Incident Map.
        '''
    )
    st.dataframe(
        assist[["Report Date", "Address", "Latitude", "Longitude"]].sort_values("Report Date", ascending=False),
        width="stretch",
        hide_index=True,
    )
    st.caption(f"{len(assist):,} Assist Agency calls in the selected date range ({assist['Latitude'].notna().sum():,} geocoded)")


# --- DATA DICTIONARY DROPDOWN -----------------------------------------------------

data_dict = load_data_dictionary()


with st.expander("Data Dictionary"):
    st.dataframe(data_dict, hide_index=True, width="stretch")

# --- IMPORTANT TEXT AT BOTTOM ----------------------------------------------------
#with st.expander("About this data", expanded=True):
st.markdown("#### Disclaimer")
st.markdown('''
**This dashboard is NOT for reporting emergencies or crimes. For an emergency, call 911.**
''')
st.caption('''
This dashboard is for informational purposes only and is not affiliated with the La Verne Police Department or the City of La Verne.
Data come from the department's public logs (https://lvpd.org/news-statistics) and were automatically processed and mapped, so locations are approximate
and information may be missing, incorrect, or updated over time. Reported incidents do not mean a crime occurred or that anyone was arrested or found responsible.
THE INFORMATION IS PROVIDED "AS IS" AND "AS AVAILABLE" WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING ANY WARRANTY OF ACCURACY, COMPLETENESS, TIMELINESS, OR FITNESS FOR A PARTICULAR PURPOSE.
Do not rely on it for legal, safety, security, employment, housing, or other decisions; you use it at your own risk and are responsible for verifying anything important with official sources.
Inclusion of an incident or address does not imply that any person or property is connected to wrongdoing.
TO THE FULLEST EXTENT PERMITTED BY LAW, LA VERNE DAILY NEWS, BRANCH FOUR INC., AND THEIR OFFICERS, EMPLOYEES, AND CONTRIBUTORS ARE NOT LIABLE FOR ANY CLAIM, LOSS, OR DAMAGE
(INCLUDING INDIRECT OR CONSEQUENTIAL DAMAGES) ARISING FROM THE USE OF, OR INABILITY TO USE, THIS DASHBOARD OR ITS DATA.
Please report mistakes to newstips@lavernedaily.org.
''')
            

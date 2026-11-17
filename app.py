import pandas as pd
import plotly.express as px
import pydeck as pdk
import streamlit as st
from datetime import date
from dateutil.relativedelta import relativedelta


st.set_page_config(page_title="Police Incidents", layout="wide")

DOW_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
# 33 incident types, so a single plotly palette (Safe has 11) would repeat colors.
# Dark24 + Light24 gives 48 distinct colors; ordered so neighbors in the list contrast.
CATEGORY_COLORS = px.colors.qualitative.Dark24 + px.colors.qualitative.Light24
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


@st.cache_data(ttl=3600)
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
    df["log_day"] = df["log_date"].dt.date
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
st.sidebar.header("Filters")
st.sidebar.caption("Use the filters below to narrow the incidents you want to see. You can filter by Date, Incident Type, or Street Address.")

# Widget labels render smaller than markdown text, so use a markdown label (same as "Incident Type")
# and hide the built-in one.
st.sidebar.markdown("**Date Range**")
date_range = st.sidebar.date_input(
    "Date Range", (min_date, max_date), min_value=min_date, max_value=max_date, label_visibility="collapsed"
)

st.sidebar.markdown("**Street Address Contains**")
address_query = st.sidebar.text_input(
    "Street Address Contains", placeholder="e.g. Foothill Blvd", label_visibility="collapsed"
)

# Buttons use on_click callbacks, which run before the widget below is built, so they can
# safely set the multiselect's session_state value.
if "incident_type_filter" not in st.session_state:
    st.session_state["incident_type_filter"] = list(natures)


def _select_all_types():
    st.session_state["incident_type_filter"] = list(natures)


def _clear_all_types():
    st.session_state["incident_type_filter"] = []



# Make the Select all / Clear all buttons smaller (they're the only buttons in the sidebar)
st.sidebar.markdown(
    """<style>
    section[data-testid="stSidebar"] .stButton button {
        min-height: 1.6rem; padding: 0.1rem 0.5rem; font-size: 0.75rem;
    }
    </style>""",
    unsafe_allow_html=True,
)
st.sidebar.markdown("**Incident Type**")
btn_all, btn_none = st.sidebar.columns(2)
btn_all.button("Select all", on_click=_select_all_types, use_container_width=True)
btn_none.button("Clear all", on_click=_clear_all_types, use_container_width=True)
selected = st.sidebar.multiselect(
    "Incident Type",
    natures,
    key="incident_type_filter",
    label_visibility="collapsed",
    placeholder="Choose one or more incident types",
)
if not selected:
    st.sidebar.caption("No incident types selected, so no incidents are shown.")


start, end = date_range if len(date_range) == 2 else (min_date, max_date)
fdf = df[df["log_day"].between(start, end) & df["grouped_nature"].isin(selected)]


# st.title("La Verne Police Incidents")
# #st.subheader(f"{filter_start.strftime('%B %d')} to {filter_end.strftime('%B %d %Y')}: {len(fdf):,} reports")
# st.divider()

# Simple substring match so partial street names work (e.g. "foothill" matches "2269 FOOTHILL BLVD")
if address_query:
    fdf = fdf[fdf["address_lc"].str.contains(address_query.strip().lower(), regex=False)]

st.title("La Verne Police Incidents")
count_col1, count_col2, count_col3 = st.columns(3)
share = f"{len(fdf) / len(df):.1%} of all incidents" if len(df) else None
count_col1.metric("Filtered Incidents", f"{len(fdf):,}", share, delta_color="off")
count_col2.metric("Total Incidents (all dates & natures)", f"{len(df):,}")
if len(fdf):
    top_counts = fdf["grouped_nature"].value_counts()
    count_col3.metric("Most Common Incident Type", str(top_counts.index[0]), f"{top_counts.iloc[0]:,} incidents",
                      delta_color="off")
else:
    count_col3.metric("Most Common Incident Type", "—")
#st.caption("This dashboard shows all incidents reported to the La Verne Police Department in the selected filters. Data are sourced from the La Verne Police Department.")

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
        st.caption("The Incident Map shows where reported incidents occurred. Hover over an incident on the map to see more information.")
        # pydeck has no built-in legend, so draw one with HTML: a colored dot per incident type
        # that is actually on the map right now (uses the same colors as the map layer)
        present_set = set(map_df["grouped_nature"].unique())
        present = [n for n in natures if n in present_set]
        st.markdown(legend_html(present), unsafe_allow_html=True)
        st.pydeck_chart(
            pdk.Deck(
                initial_view_state=pdk.ViewState(latitude=map_df["lat"].mean(), longitude=map_df["lon"].mean(), zoom=13),
                layers=[
                    pdk.Layer(
                        "ScatterplotLayer",
                        data=map_df,
                        get_position=["lon", "lat"],
                        get_fill_color="color",
                        get_radius=28,
                        pickable=True,
                    )
                ],
                tooltip={"text": "{date}\n{grouped_nature}\n{incident_address}"},
                map_style="light",
            )
        )
    else:
        st.info("Map unavailable — run `geocode_neighborhoods.py` then `geocode_census_fallback.py` to add coordinates.")
    
    
    st.caption(f"Note: Only incidents with full addresses are mapped. {fdf[fdf['lat'].isna()].shape[0]} incidents are missing full addresses and are not mapped.")


    # ── Daily chart ────────────────────────────────────────────────────────────────
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
        fig.add_scatter(x=daily["log_date"], y=daily["rolling_7"], name="7-day avg", line=dict(color="#1a4a8a", width=2.5))

    fig.update_layout(height=350, margin=dict(t=10, b=10))
    if not breakdown:
        # one tooltip with both the daily count and the 7-day average. (Not used for the breakdown:
        # with 30+ stacked types a unified tooltip would be far too long.)
        fig.update_layout(hovermode="x unified")
    if breakdown:
        # Plotly can't lay a legend out in columns and scrolls (or stretches the chart) with 30+
        # entries, so hide it and show the compact wrapping legend above the chart instead.
        fig.update_layout(showlegend=False)
        st.markdown(legend_html([n for n in natures if n in set(daily["grouped_nature"])]), unsafe_allow_html=True)
    # Tick labels adapt to the zoom level: days when the range is short, months when it's long.
    # (dtickrange is the tick spacing in milliseconds; "M1" etc. are month steps.)
    fig.update_xaxes(
        tickformatstops=[
            dict(dtickrange=[None, 7 * 86400000], value="%B %d, %Y"),   # up to ~weekly ticks
            dict(dtickrange=[7 * 86400000, "M1"], value="%B %d"),       # weekly to monthly ticks
            dict(dtickrange=["M1", None], value="%B %Y"),               # monthly or wider
        ],
        hoverformat="%A, %B %d, %Y",  # hovering always shows the full date, e.g. "Monday, April 06, 2026"
    )
    st.plotly_chart(fig, use_container_width=True)

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
        category_orders={"hour_label": HOUR_LABELS, "dow": DOW_ORDER},
        labels={"hour_label": "Hour of Day", "dow": "", "count": "Incidents"},
    )
    fig_heat.update_layout(height=320, margin=dict(t=10, b=10))
    st.plotly_chart(fig_heat, use_container_width=True)



    # ── Table ────────────────────────────────────────────────────────────────
    st.subheader("All Incidents")
    st.caption("The table lists all reported incidents.")

    fdf = fdf[['reported', 'grouped_nature', 'nature_description', 'incident_address']].rename({'reported': 'Report Date', 
                      'grouped_nature': 'Incident Type', 
                      'nature_description': 'Description', 
                      'incident_address': 'Address'}, axis = 1)

    # # Add a search bar
    # search_query = st.text_input("Search the dataframe:")

    # # Filter the dataframe based on the search query
    # if search_query:
    #     filtered_df = fdf[
    #         fdf['Incident Type'].str.contains(search_query, case=False, na=False) |
    #         fdf['Description'].str.contains(search_query, case=False, na=False) |
    #         fdf['Address'].str.contains(search_query, case=False, na=False)
    #     ]
    # else:
    #     filtered_df = fdf

    table_df = fdf[['Report Date', 'Incident Type', 'Description', 'Address']].reset_index(drop = True)

    st.dataframe(
        table_df,
        height=600,
        use_container_width=True,
        hide_index=True,
        column_config={"Report Date": st.column_config.DatetimeColumn("Report Date", format="MMM D, YYYY h:mm a")},
    )


# ── Assist Agency dropdown ───────────────────────────────────────────────────────
# Pulled out separately since these get folded into "Welfare/Assist" above and are
# easy to miss. Also a lot of these are calls where LVPD helped a *different* agency
# at an address in another city (not La Verne) — worth reviewing on their own rather
# than plotted on the map, since we can't assume they're actually here.
#st.subheader("Assist Agency Calls")


assist = df[df["is_assist"] & df["log_day"].between(start, end)]

if address_query:
    assist = assist[assist["address_lc"].str.contains(address_query.strip().lower(), regex=False)]

assist = assist.rename({'reported': 'Report Date', 
                  'grouped_nature': 'Incident Type', 
                  'nature_description': 'Description', 
                  'incident_address': 'Address', 
                  'lat': 'Latitude', 
                  'lon': 'Longitude'}, axis = 1)

with st.expander("View Assist Agency Calls"):
    st.caption('''
        The table lists all Assist Agency Calls. 'Assist Agency' are incidents where the La Verne police department 
        assists a nearby police department with an incident, or a nearby police department assists La Verne PD with an incident.
        Note: Raw \"ASSIST AGENCY\" incidents, are shown separately from the Incident Type filter.
        Addresses without lat/lon below are excluded from the Incident Map.
        '''
    )
    st.dataframe(
        assist[["Report Date", "Address", "Latitude", "Longitude"]].sort_values("Report Date", ascending=False),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(f"{len(assist):,} Assist Agency calls in the selected date range ({assist['Latitude'].notna().sum():,} geocoded)")


# --- DATA DICTIONARY DROPDOWN -----------------------------------------------------

data_dict = load_data_dictionary()


with st.expander("View Data Dictionary"):
    st.dataframe(data_dict, hide_index=True, use_container_width=True)

# --- IMPORTANT TEXT AT BOTTOM ----------------------------------------------------
st.subheader("Important")
st.markdown('''
            * Data are sourced from the La Verne Police Department at https://lvpd.org/news-statistics.
            * This dashboard shows reported incidents and is intended for informational purposes only.
            * This dashboard may make mistakes. 
            * Reported incidents do not necessarily mean that a crime occurred or that anyone was arrested or found responsible.
            * Incident information may be updated or corrected over time.
            * This dashboard is not for reporting emergencies or crimes. For an emergency, call 911.
            
            ''')
            

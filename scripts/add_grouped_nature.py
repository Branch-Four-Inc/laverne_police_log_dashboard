#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas"]
# ///
import pandas as pd

GROUPS = {
    "Fraud": ["470 PC FRAUD", "537E PC DEFRAUD"],
    "Grand Theft": ["487 PC", "GTARPT", "GRAND THEFT VEH", "GTA IP/JO", "GTA LOCATED"],
    "Vehicle Theft": ["THEFT VEHICLE"],
    "Vandalism": ["594 PC GRAFFITI", "594PC VANDALISM", "VANDALISM IP/JO"],
    "Burglary": ["459PC BURGLARY", "BURGLARY IP/JO", "459PC VEH/BURG"],
    "Robbery": ["211PC ROBBERY", "ROBBERY IP/JO"],
    "ADW": ["245PC ADW IP/JO", "245PC ADW RPT"],
    "Battery": ["BATTERY", "BATTERY IP/JO"],
    "Suspicious": ["SUSP CIRCS", "PDOBS", "SUSP SUBJ","SUSP VEHICLE"],
    
    # "Suspicious Circumstances": ["SUSP CIRCS", "PDOBS"],
    # "Suspicious Subjects": ["SUSP SUBJ"],
    # "Suspicious Vehicle": ["SUSP VEHICLE"],
    "Traffic Collision": ["TC NON INJURY", "TC HR NON INJ", "TC INJURY", "TC HIT RUN INJ", "TC VEH VS PED", "TC UNK"],
    "Petty Theft": ["PETTY THEFT", "PETTY THEFT RPT"],
    "Theft": ["THEFT MAIL", "IDENTITY THEFT"],
    "Disturbance": [
        "DISTURB PERSON",
        "DISTURB SUBJS",
        "DISTURB NOISE",
        "DISTURB MUSIC",
        "DISTURB PARTY",
        "DISTURB ROAD",
        "DISTURB UNK",
        "DISTURB SHOTS",
        "DISTURB FIREWKS",
    ],
    "Threatening Calls": ["653M PC"],
    "Fight": ["FIGHT IP/JO"],
    "Impound": ["PPI"],
    "Traffic Stop": ["TRAFFIC STOP"],
    "Traffic Enforcement": ["TRAFFIC ENFORCE", "TRAFFIC HAZARD", "BIKE TRAFFIC", "SPEEDING VEH", "PEDESTRIAN TRAF"],
    "Parking": ["ILLEGAL PARKER", "ABAND VEH"],
    "Suspicious Activity": ["SUSP TRANS", "SUSP NOISE"],
    "Drug/Alcohol": ["DRUG ACTIVITY", "DUI", "PUBLIC INTOX"],
    "Animal": [
        "ANIMAL BITE RPT",
        "ANIMAL CRUELTY",
        "ANIMAL INJURED",
        "ANIMAL VICIOUS",
        "BARKING DOG",
        "HUMANE PROBLEM",
        "BEAR PROB",
    ],
    "Property": ["FOUND PROP", "LOST PROPERTY", "PROP FOR DESTR", "VEH TAMPERING"],
    "Welfare/Assist": [
        "WELFARE CHECK",
        "CITIZEN ASST",
        "CODE H SUBJ",
        "ASSIST AGENCY",
        "APS REF",
        "KEEP THE PEACE",
        "HAIL BY CITIZEN",
    ],
    "Trespass/Solicitor": ["TRESPASS", "SOLICITOR"],
    "Shoplifting": ["SHOPLIFTER IC"],
    "Other": [
        "911 INVEST",
        "AREA CHECK",
        "FOOT PATROL",
        "CITE SIGN OFF",
        "LA VERNE MUNI",
        "COMM BUILD CHK",
        "MISSING ADULT",
        "MISSING FOUND",
        "INDECENT EXPOS",
        "BRANDISH WEAPON",
        "BOMB THREAT",
        "PROWLER",
        "CRIM THRT RPT",
        "ILLEGAL BURN",
        "SUSP CIRCS",
        "TRAFFIC STOP",
    ],
}

nature_to_group = {nature: group for group, natures in GROUPS.items() for nature in natures}

df = pd.read_csv("daily_logs.csv")
df["grouped_nature"] = df["nature"].map(nature_to_group).fillna(df["nature"])
df.to_csv("daily_logs.csv", index=False)

## FOR FUTURE: CLEAN UNMERGED NATURES TO THEIR CORRECT NATURE SPELLING


print(df["grouped_nature"].value_counts().to_string())
print(f"\nUnmapped natures: {df[~df['nature'].isin(nature_to_group)]['nature'].dropna().unique().tolist()}")

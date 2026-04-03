import os
import pandas as pd
import sys
import argparse

def calculate_metrics(df, period_name):
    """Calculate diabetes metrics for a given dataframe."""
    if df.empty:
        return {}

    bg = df["CGM"]
    total_samples = len(bg)

    # Basic metrics
    tir = ((bg >= 70) & (bg <= 180)).sum() / total_samples * 100
    below70 = (bg < 70).sum() / total_samples * 100
    below54 = (bg < 54).sum() / total_samples * 100
    above180 = (bg > 180).sum() / total_samples * 100
    above250 = (bg > 250).sum() / total_samples * 100
    mean_bg = bg.mean()
    cv = bg.std() / mean_bg * 100 if mean_bg > 0 else 0
    total_basal = df["basal_rate"].sum()
    total_bolus = df["bolus"].sum()
    total_insulin = total_basal + total_bolus

    # Additional insights
    std_bg = bg.std()
    min_bg = bg.min()
    max_bg = bg.max()
    median_bg = bg.median()
    q25_bg = bg.quantile(0.25)
    q75_bg = bg.quantile(0.75)

    return {
        "Patient": f"{df['Patient'].iloc[0]}_{period_name}" if 'Patient' in df.columns else f"{period_name}",
        "Period": period_name,
        "TIR_70_180 (%)": round(tir, 1),
        "<70 (%)": round(below70, 2),
        "<54 (%)": round(below54, 2),
        ">180 (%)": round(above180, 1),
        ">250 (%)": round(above250, 2),
        "Mean_BG": round(mean_bg, 1),
        "SD_BG": round(std_bg, 1),
        "Min_BG": round(min_bg, 1),
        "Max_BG": round(max_bg, 1),
        "Median_BG": round(median_bg, 1),
        "Q25_BG": round(q25_bg, 1),
        "Q75_BG": round(q75_bg, 1),
        "CV_%": round(cv, 1),
        "Total_Basal_U": round(total_basal, 3),
        "Total_Bolus_U": round(total_bolus, 3),
        "Total_Insulin_U": round(total_insulin, 3),
        "Samples": total_samples
    }

def check_missing_samples(df):
    """Check for missing samples in the data."""
    if df.empty:
        return "No data"

    minutes = df["minutes_from_start"]
    expected_interval = 3.0
    diffs = minutes.diff().dropna()
    missing_intervals = ((diffs - expected_interval) / expected_interval).astype(int)
    total_missing = missing_intervals.sum()

    max_min = minutes.max()
    expected_samples = int(max_min / 3) + 1
    actual_samples = len(df)

    if actual_samples != expected_samples:
        return f"Expected {expected_samples} samples, got {actual_samples}. Missing: {expected_samples - actual_samples}"
    elif total_missing > 0:
        return f"Missing {total_missing} intervals"
    else:
        return "Complete"

def process_patient_file(file_path):
    """Process a single patient CSV file."""
    try:
        df = pd.read_csv(file_path)
        if df.empty:
            return []

        # Check if required columns exist
        required_cols = ["CGM", "basal_rate", "bolus"]
        if not all(col in df.columns for col in required_cols):
            print(f"Skipping {file_path}: missing required columns")
            return []

        # If minutes_from_start not present, try to infer from index or time
        if "minutes_from_start" not in df.columns:
            if "Time" in df.columns:
                # Assume Time is datetime, calculate minutes from start
                df["Time"] = pd.to_datetime(df["Time"])
                start_time = df["Time"].min()
                df["minutes_from_start"] = (df["Time"] - start_time).dt.total_seconds() / 60
            else:
                # Assume 3 min intervals
                df["minutes_from_start"] = df.index * 3

        # Add patient name
        patient_name = os.path.splitext(os.path.basename(file_path))[0]
        df['Patient'] = patient_name

        max_min = df["minutes_from_start"].max()
        days = max_min / 1440

        # Reliable available day count for 3-min samples: 1 day=0..1437, 3 day=0..4317, 7 day=0..10077
        # Use floor(days) + 1 so 6.998 becomes 7 and we handle inclusive interval endpoints.
        available_days = int(days) + 1

        periods = []
        if available_days >= 7:
            periods = [1, 3, 7]
        elif available_days >= 3:
            periods = [1, 3]
        else:
            periods = [1]

        summaries = []
        missing_check = check_missing_samples(df)

        for period_days in periods:
            period_min = period_days * 1440
            period_df = df[df["minutes_from_start"] <= period_min]
            if not period_df.empty:
                metrics = calculate_metrics(period_df, f"{period_days}day")
                metrics["Missing_Samples_Check"] = missing_check
                summaries.append(metrics)

        return summaries
    except Exception as e:
        print(f"Error processing {file_path}: {e}")
        return []

def main():
    parser = argparse.ArgumentParser(description="Generate diabetes simulation summary from patient CSV files.")
    parser.add_argument("folder_path", help="Path to the folder containing patient CSV files")
    parser.add_argument("--output", default="SUMMARY_ALL_PATIENTS.csv", help="Output CSV file name")
    args = parser.parse_args()

    folder_path = args.folder_path
    if not os.path.isdir(folder_path):
        print(f"Error: {folder_path} is not a valid directory")
        sys.exit(1)

    # Find all CSV files
    csv_files = [f for f in os.listdir(folder_path) if f.endswith('.csv') and not f.startswith('SUMMARY')]

    all_summaries = []
    for csv_file in csv_files:
        file_path = os.path.join(folder_path, csv_file)
        summaries = process_patient_file(file_path)
        all_summaries.extend(summaries)

    if not all_summaries:
        print("No valid data found")
        sys.exit(1)

    # Create summary dataframe
    summary_df = pd.DataFrame(all_summaries)

    # Save to CSV
    output_path = os.path.join(folder_path, args.output)
    summary_df.to_csv(output_path, index=False)

    # Print results
    print("\n" + "="*120)
    print(summary_df.to_string(index=False, float_format=lambda x: f"{x:5.1f}" if isinstance(x, float) else str(x)))
    print("="*120)
    print(f"All summaries saved in {output_path}")

if __name__ == "__main__":
    main()
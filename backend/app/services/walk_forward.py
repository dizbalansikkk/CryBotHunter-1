import pandas as pd


def walk_forward_windows(frame, train_days=90, test_days=30, minimum_folds=2):
    times = pd.to_datetime(frame["timestamp"], utc=True)
    if not times.is_monotonic_increasing or times.duplicated().any():
        raise ValueError("Walk-forward requires unique chronological timestamps")
    if not times.diff().dropna().eq(pd.Timedelta(hours=1)).all():
        raise ValueError("Walk-forward requires contiguous hourly bars")
    folds = []
    start = times.iloc[0]
    boundary = start + pd.Timedelta(days=train_days)
    while boundary + pd.Timedelta(days=test_days) <= times.iloc[-1] + pd.Timedelta(hours=1):
        # Purge one complete bar at the boundary; next-return labels must not
        # reach into a holdout. Every environment liquidates on its last bar.
        train = frame[(times >= boundary - pd.Timedelta(days=train_days)) & (times < boundary - pd.Timedelta(hours=1))]
        test = frame[(times >= boundary) & (times < boundary + pd.Timedelta(days=test_days))]
        if len(train) < 1000 or len(test) < 300:
            raise ValueError("Insufficient contiguous observations in walk-forward fold")
        folds.append((train.reset_index(drop=True), test.reset_index(drop=True)))
        boundary += pd.Timedelta(days=test_days)
    if len(folds) < minimum_folds:
        raise ValueError("At least two complete 90-day/30-day walk-forward folds required")
    return folds

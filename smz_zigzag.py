import pandas as pd
import numpy as np
import plotly.graph_objects as go

def add_zigzag_to_fig(fig, df: pd.DataFrame):
    """S.M.Z sequential candlestick-by-candlestick ZigZag code."""
    if len(df) < 2 or 'high' not in df.columns or 'low' not in df.columns:
        return fig
    
    highs, lows = df['high'].to_numpy(), df['low'].to_numpy()
    indices = df.index.to_numpy()
    pivots = []
    
    direction = "up" if highs[1] >= highs[0] else "down"
    last_pivot_val = highs[0] if direction == "up" else lows[0]
    pivots.append({"index": indices[0], "value": last_pivot_val, "type": direction})
    
    curr_extreme_idx = 0
    
    for i in range(1, len(df)):
        h, l = highs[i], lows[i]
        prev_h, prev_l = highs[i-1], lows[i-1]
        is_outside = h > prev_h and l < prev_l
        
        if direction == "down":
            if l < lows[curr_extreme_idx]:
                curr_extreme_idx = i
            if h > highs[curr_extreme_idx] and not is_outside:
                pivots.append({"index": indices[curr_extreme_idx], "value": lows[curr_extreme_idx], "type": "low"})
                direction = "up"
                curr_extreme_idx = i
            elif is_outside and i + 1 < len(df) and highs[i+1] > h:
                pivots.append({"index": indices[i], "value": l, "type": "low"})
                direction = "up"
                curr_extreme_idx = i
                
        elif direction == "up":
            if h > highs[curr_extreme_idx]:
                curr_extreme_idx = i
            if l < lows[curr_extreme_idx] and not is_outside:
                pivots.append({"index": indices[curr_extreme_idx], "value": highs[curr_extreme_idx], "type": "high"})
                direction = "down"
                curr_extreme_idx = i
            elif is_outside and i + 1 < len(df) and lows[i+1] < l:
                pivots.append({"index": indices[i], "value": h, "type": "high"})
                direction = "down"
                curr_extreme_idx = i
                
    if pivots:
        pivot_x = [p['index'] for p in pivots]
        pivot_y = [p['value'] for p in pivots]
        fig.add_trace(go.Scatter(x=pivot_x, y=pivot_y, mode='lines', line=dict(color='#6E7E95', width=2), name='S.M.Z ZigZag'))
        fig.add_trace(go.Scatter(x=pivot_x, y=pivot_y, mode='markers', marker=dict(color='#1f77b4', size=6, symbol='circle'), showlegend=False))
        
    return fig

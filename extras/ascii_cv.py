import argparse
import os
import pandas as pd
import numpy as np

DEFAULT_DTA = os.environ.get("GAMRY_DTA_PATH", "RCV.DTA")

def parse_gamry_dta(file_path):
    with open(file_path, 'r') as f:
        lines = f.readlines()
        
    data_start = -1
    for i, line in enumerate(lines):
        if line.startswith('CURVE1'):
            data_start = i + 3
            break
            
    data_lines = []
    if data_start != -1:
        for line in lines[data_start:]:
            parts = line.strip().split()
            if len(parts) >= 5:
                try:
                    pt = int(parts[0])
                    t = float(parts[1])
                    vf = float(parts[2])
                    im = float(parts[3])
                    data_lines.append([pt, t, vf, im])
                except ValueError:
                    continue
                
    df = pd.DataFrame(data_lines, columns=['Pt', 'T', 'Vf', 'Im'])
    return df

def ascii_plot(x, y, width=80, height=40):
    min_x, max_x = min(x), max(x)
    min_y, max_y = min(y), max(y)
    
    grid = [[' ' for _ in range(width)] for _ in range(height)]
    
    for vx, vy in zip(x, y):
        ix = int(round((vx - min_x) / (max_x - min_x + 1e-9) * (width - 1)))
        iy = int(round((vy - min_y) / (max_y - min_y + 1e-9) * (height - 1)))
        grid[height - 1 - iy][ix] = '*'
        
    print(f"X range: {min_x:.3f} to {max_x:.3f} V")
    print(f"Y range: {min_y:.3f} to {max_y:.3f} uA")
    for row in grid:
        print("".join(row))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ASCII plot of Gamry RCV.DTA file")
    parser.add_argument("file_path", nargs="?", default=DEFAULT_DTA, help="Path to Gamry .DTA file")
    args = parser.parse_args()
    df = parse_gamry_dta(args.file_path)
    df['Im_uA'] = df['Im'] * 1e6
    voltage = df['Vf'].values
    current = df['Im_uA'].values
    ascii_plot(voltage, current)
    print("Max current", np.max(current), "at", voltage[np.argmax(current)])
    print("Min current", np.min(current), "at", voltage[np.argmin(current)])

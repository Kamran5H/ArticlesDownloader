import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

file_path = r"C:\Users\Public\Documents\My Gamry Data\RCV.DTA"

def parse_gamry_dta(file_path):
    with open(file_path, 'r') as f:
        lines = f.readlines()
        
    metadata = {}
    data_start = -1
    for i, line in enumerate(lines):
        if line.startswith('SCANRATE'):
            parts = line.split()
            if len(parts) >= 3:
                metadata['SCANRATE'] = float(parts[2])
        if line.startswith('AREA'):
            parts = line.split()
            if len(parts) >= 3:
                metadata['AREA'] = float(parts[2])
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
    return df, metadata

df, metadata = parse_gamry_dta(file_path)

if df.empty:
    print("No data found in the file.")
    exit(1)

# Convert current to uA
df['Im_uA'] = df['Im'] * 1e6

voltage = df['Vf'].values
current = df['Im_uA'].values

# Max and Min values in the scan (since no distinct faradaic peaks exist)
Ipa_idx = np.argmax(current)
Ipc_idx = np.argmin(current)

Ipa = current[Ipa_idx]
Epa = voltage[Ipa_idx]
Ipc = current[Ipc_idx]
Epc = voltage[Ipc_idx]

E_half = (Epa + Epc) / 2
Delta_E = abs(Epa - Epc) * 1000 # in mV
I_ratio = abs(Ipa / Ipc) if Ipc != 0 else np.nan

mass = 291.03

# Use a clean grid style
plt.style.use('seaborn-v0_8-whitegrid')
fig = plt.figure(figsize=(20, 10), dpi=300)

# Use GridSpec to perfectly separate the plot from the text
gs = fig.add_gridspec(1, 2, width_ratios=[2.5, 1.2], wspace=0.05)

ax_plot = fig.add_subplot(gs[0])
ax_text = fig.add_subplot(gs[1])
ax_text.axis('off') # Hide axes for text region

# Plotting the CV
ax_plot.plot(voltage, current, linewidth=3.5, color='#1f77b4', label='Cyclic Voltammogram')
ax_plot.scatter([Epa], [Ipa], color='#d62728', s=150, zorder=5, label='Maximum Anodic Current')
ax_plot.scatter([Epc], [Ipc], color='#2ca02c', s=150, zorder=5, label='Maximum Cathodic Current')

ax_plot.set_title("Cyclic Voltammetry Analysis", fontsize=26, fontweight='bold', pad=20, color='#2c3e50')
ax_plot.set_xlabel("Potential (V vs. Ref)", fontsize=20, fontweight='bold')
ax_plot.set_ylabel("Current (µA)", fontsize=20, fontweight='bold')
ax_plot.tick_params(axis='both', which='major', labelsize=16)

# Legend inside the plot, but controlled
ax_plot.legend(loc='best', fontsize=16, frameon=True, shadow=True, facecolor='white', edgecolor='black')

# Enhance plot borders
for spine in ax_plot.spines.values():
    spine.set_linewidth(2.5)
    spine.set_color('#34495e')

scan_rate = metadata.get('SCANRATE', 'Unknown')

# Comprehensive Text Box for the right panel
info_text = (
    "====== SYSTEM & COMPOUND ======\n"
    f"Compound Mass : {mass} g/mol\n"
    f"Scan Rate     : {scan_rate} mV/s\n\n"
    
    "====== EXTRACTED METRICS ======\n"
    f"Max Anodic E  (Epa) : {Epa:+.3f} V\n"
    f"Max Anodic I  (Ipa) : {Ipa:+.2f} µA\n"
    f"Max Cathodic E (Epc): {Epc:+.3f} V\n"
    f"Max Cathodic I (Ipc): {Ipc:+.2f} µA\n\n"
    
    "====== DERIVED VALUES ======\n"
    f"Midpoint Pot. (E1/2): {E_half:+.3f} V\n"
    f"Peak Sep. (ΔEp)     : {Delta_E:.1f} mV\n"
    f"Current Ratio       : {I_ratio:.2f}\n\n"

    "====== SCIENTIFIC REASONING ======\n"
    "1. Shape & Capacitance:\n"
    "The CV exhibits a roughly rectangular,\n"
    "featureless shape lacking sharp,\n"
    "diffusion-controlled faradaic peaks.\n"
    "This indicates predominantly capacitive\n"
    "(double-layer) behavior, characteristic\n"
    "of blank electrolytes or carbon-based\n"
    "materials.\n\n"
    
    "2. Edge Currents:\n"
    "The maximum currents (Epa/Epc) occur\n"
    "at the scan boundaries (+0.6V, -0.2V).\n"
    "This is typically due to the onset of\n"
    "solvent breakdown or irreversible\n"
    "oxidation/reduction at the limits.\n\n"
    
    "3. Reversibility & E1/2:\n"
    "Because no distinct redox couple exists,\n"
    "classical parameters like E1/2, ΔEp,\n"
    "and Reversibility are mathematically\n"
    "derived from the boundaries but do not\n"
    "represent a Nernstian faradaic process.\n"
)

# Place text box in the text axis
props = dict(boxstyle='round,pad=1', facecolor='#f8f9fa', alpha=1, edgecolor='#bdc3c7', linewidth=2)
ax_text.text(0.0, 0.95, info_text, transform=ax_text.transAxes, fontsize=15,
        verticalalignment='top', bbox=props, family='monospace', color='#2c3e50', linespacing=1.5)

plt.tight_layout()

output_path = r"C:\Users\chkam\OneDrive\Desktop\CV_Analysis_Report.pdf"
plt.savefig(output_path, format='pdf', bbox_inches='tight')
print(f"Report saved to {output_path}")

print("Starting import test...")

print("Testing numpy...")
import numpy
print("numpy OK")

print("Testing matplotlib...")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
print("matplotlib OK")

print("Testing RDKit...")
from rdkit import Chem
from rdkit.Chem import AllChem
print("RDKit OK")

print("Testing PubChemPy...")
import pubchempy
print("PubChemPy OK")

print("Testing python-pptx...")
from pptx import Presentation
print("python-pptx OK")

print("Testing pywin32...")
try:
    import win32com.client
    print("pywin32 OK")
except Exception as e:
    print("pywin32 failed:", e)

print("All import tests completed.")
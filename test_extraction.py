import os
import json
import mimetypes
import google.generativeai as genai

# Load API key
CONFIG_FILE = "config.json"
api_key = ""
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
        api_key = config.get("api_key", "")

if not api_key:
    print("API Key not found in config.json")
    exit(1)

genai.configure(api_key=api_key)
model = genai.GenerativeModel('gemini-3.5-flash-lite')

# Path to a sample file
sample_file = r"d:\Madeinchina\supplier_data\Screenshot 2026-08-01 150515.png"
print(f"Testing extraction on: {sample_file}")

try:
    print("Reading file...")
    mime_type, _ = mimetypes.guess_type(sample_file)
    with open(sample_file, "rb") as f:
        file_bytes = f.read()
    
    prompt = "Extract the supplier name and product names from this quotation. Return in plain text."
    print(f"Generating content using {mime_type}...")
    response = model.generate_content([
        {
            "mime_type": mime_type,
            "data": file_bytes
        },
        prompt
    ])
    print("Response text:")
    print(response.text)
    print("Success!")
except Exception as e:
    import traceback
    print("Error occurred:")
    traceback.print_exc()

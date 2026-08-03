# AI Supplier Quote Extractor Desktop App

A modern dark-themed Python desktop application that uses the Google Gemini API to scan a folder of supplier quotations (in PDF, PNG, JPG, JPEG, or TXT formats) and automatically extract the structured quote data into an editable table, which can then be exported to Excel or CSV.

## Requirements
* Python 3.8 or higher.
* A Google Gemini API Key. You can get a free key from [Google AI Studio](https://aistudio.google.com/).

## Dependencies
The application will automatically detect and offer to install missing dependencies on first launch. If you prefer to install them manually, run:
```bash
pip install customtkinter pandas openpyxl google-generativeai pillow
```

## Running the Application
To launch the desktop application, open your terminal/command prompt in this folder and run:
```bash
python app.py
```

## Features
1. **API Key Setup:** Enter your Gemini API key in the configuration panel on the left, save it (it saves locally to a `config.json` file), and verify it works with the "Save & Test Key" button.
2. **Select Quotes Folder:** Click the folder selection button to load your folder containing supplier quotations (like the `supplier_data` folder).
3. **Queue Processing:** Click "Start Extraction" to run the AI engine on all valid files. The progress bar will indicate the status.
4. **Editable Data Table:** The extracted data appears in the main table.
   * **Edit:** Double-click any row to edit values manually (fix supplier names, adjust specifications, correct prices, etc.).
   * **Add:** Click `+ Add Row` to add a new row manually.
   * **Delete:** Click `- Delete Row` to remove selected entries.
5. **Excel & CSV Export:** Click "Export to Excel" or "Export to CSV" to save the consolidated table. The Excel output is automatically styled and formatted.

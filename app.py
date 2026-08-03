import os
import sys
import json
import sqlite3
import threading
import subprocess

# --- Auto-dependency installer ---
def install_dependencies():
    # Added pymupdf and matplotlib for PDF page rendering and plotting
    required = {"customtkinter", "pandas", "openpyxl", "google-generativeai", "pillow", "pymupdf", "matplotlib"}
    try:
        import pkg_resources
        installed = {pkg.key for pkg in pkg_resources.working_set}
    except Exception:
        try:
            import importlib.metadata
            installed = {dist.metadata['Name'].lower() for dist in importlib.metadata.distributions()}
        except Exception:
            installed = set()
            
    missing = required - installed
    if missing:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        ans = messagebox.askyesno(
            "Install Dependencies",
            f"The following required libraries are missing: {', '.join(missing)}.\n\nWould you like the app to install them automatically now?"
        )
        if ans:
            progress = tk.Toplevel()
            progress.title("Installing...")
            progress.geometry("350x120")
            progress.resizable(False, False)
            # Center the window
            screen_width = progress.winfo_screenwidth()
            screen_height = progress.winfo_screenheight()
            x = (screen_width / 2) - (350 / 2)
            y = (screen_height / 2) - (120 / 2)
            progress.geometry(f"+{int(x)}+{int(y)}")
            
            lbl = tk.Label(progress, text="Installing dependencies, please wait...\n(This might take a minute)", pady=20, font=("Segoe UI", 10))
            lbl.pack()
            progress.update()
            
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
                messagebox.showinfo("Success", "All dependencies installed successfully! Starting the app...")
                progress.destroy()
                root.destroy()
                # Restart the script
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to install dependencies: {e}\nPlease run: pip install customtkinter pandas openpyxl google-generativeai pillow pymupdf matplotlib")
                sys.exit(1)
        else:
            sys.exit(0)

# Check and install dependencies
install_dependencies()

# --- Main App Imports ---
import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import pandas as pd
from PIL import Image
from google import genai
from google.genai import types
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import webbrowser

# --- Configurations ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
DB_FILE = os.path.join(SCRIPT_DIR, "quotes.db")

# --- Database Initialization ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Table for tracking successfully processed files
    c.execute("""
        CREATE TABLE IF NOT EXISTS processed_files (
            filename TEXT PRIMARY KEY,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Table for storing all quotation metrics
    c.execute("""
        CREATE TABLE IF NOT EXISTS extracted_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            supplier TEXT,
            product TEXT,
            spec TEXT,
            color TEXT,
            elastic TEXT,
            price REAL,
            unit TEXT,
            moq TEXT,
            packing TEXT,
            term TEXT,
            lead_time TEXT
        )
    """)
    # Table for supplier contacts
    c.execute("""
        CREATE TABLE IF NOT EXISTS supplier_contacts (
            supplier TEXT PRIMARY KEY,
            contact_info TEXT,
            source_file TEXT,
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        c.execute("ALTER TABLE supplier_contacts ADD COLUMN source_file TEXT")
    except sqlite3.OperationalError:
        pass
    
    # Self-healing migration for validity_date and sourcing_risk columns in quotes table
    try:
        c.execute("ALTER TABLE extracted_quotes ADD COLUMN validity_date TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE extracted_quotes ADD COLUMN sourcing_risk TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE extracted_quotes ADD COLUMN attached_media TEXT")
    except sqlite3.OperationalError:
        pass
        
    # Table for chatbot history persistence
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT,
            message TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    
    # Auto-populate contacts from existing quotes
    c.execute("SELECT DISTINCT supplier FROM extracted_quotes WHERE supplier IS NOT NULL AND supplier != 'Unknown'")
    suppliers = [row[0] for row in c.fetchall()]
    for s in suppliers:
        c.execute("SELECT filename FROM extracted_quotes WHERE supplier = ? LIMIT 1", (s,))
        q_row = c.fetchone()
        filename = q_row[0] if q_row else "N/A"
        
        c.execute("SELECT COUNT(*) FROM supplier_contacts WHERE supplier = ?", (s,))
        if c.fetchone()[0] == 0:
            c.execute("INSERT INTO supplier_contacts (supplier, contact_info, source_file) VALUES (?, ?, ?)", 
                      (s, "Contact info placeholder. Click '✏ Edit Info' to fill manually, or process a new quote from this supplier to auto-extract details.", filename))
        else:
            c.execute("UPDATE supplier_contacts SET source_file = ? WHERE supplier = ? AND (source_file IS NULL OR source_file = '')", (filename, s))
    conn.commit()
    conn.close()

class App(ctk.CTk):
    def generate_with_fallback(self, content_list, prompt, json_response=True):
        models_to_try = [
            'gemini-3.1-flash-lite',
            'gemini-3.5-flash-lite',
            'gemini-3.6-flash',
            'gemini-2.0-flash-lite',
            'gemini-2.0-flash'
        ]
        
        # Caching optimization: Try the last successful model first!
        if hasattr(self, 'last_working_model') and self.last_working_model in models_to_try:
            models_to_try.remove(self.last_working_model)
            models_to_try.insert(0, self.last_working_model)
            
        last_error = None
        for model_name in models_to_try:
            for attempt in range(3):
                try:
                    client = genai.Client(api_key=self.api_key)
                    contents = []
                    for item in content_list:
                        if isinstance(item, dict) and "data" in item:
                            part = types.Part.from_bytes(
                                data=item["data"],
                                mime_type=item["mime_type"]
                            )
                            contents.append(part)
                        else:
                            contents.append(item)
                    
                    contents.append(prompt)
                    
                    config_params = {}
                    if json_response:
                        config_params["response_mime_type"] = "application/json"
                    
                    config = types.GenerateContentConfig(**config_params)
                    
                    response = client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=config
                    )
                    
                    # Successfully generated content! Save as last working model
                    self.last_working_model = model_name
                    return response.text
                except Exception as e:
                    last_error = e
                    err_msg = str(e).lower()
                    
                    # Check for quota or credit depletion error
                    if "resource_exhausted" in err_msg or "429" in err_msg or "quota" in err_msg or "credits are depleted" in err_msg or "unsupported location" in err_msg or "unsupported region" in err_msg or "unsupported api key" in err_msg:
                        # If the quota limit is 0 (blocked on this model), don't retry, just proceed to next model!
                        if "limit: 0" in err_msg:
                            break
                        
                        import time
                        print(f"Model {model_name} rate limit (429) hit. Sleeping 10s before retry (Attempt {attempt+1}/3)...")
                        time.sleep(10.0)
                        continue
                    else:
                        print(f"Model {model_name} failed: {e}")
                        break
        raise last_error

    def __init__(self):
        super().__init__()

        self.title("AI Supplier Quote Extractor")
        self.geometry("1400x750")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.api_key = ""
        self.selected_folder = ""
        self.files_list = []
        self.extracted_data = [] # List of dicts
        self.quote_id_counter = 1
        self.is_extracting = False
        self.spinner_idx = 0
        self.current_preview_path = ""

        # Chat popup loading variables
        self.chat_is_extracting = False
        self.chat_spinner_idx = 0

        # Initialize database tables
        init_db()

        # Configure grid layout: Left (0), Middle (1, weight 1), Right (2)
        self.grid_columnconfigure(0, weight=0, minsize=320)
        self.grid_columnconfigure(1, weight=3)
        self.grid_columnconfigure(2, weight=0, minsize=360)
        self.grid_rowconfigure(0, weight=1)

        # --- LEFT PANEL: Control & Files list ---
        self.left_frame = ctk.CTkFrame(self, width=320, corner_radius=0)
        self.left_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.left_frame.grid_rowconfigure(3, weight=1)

        # Title Label
        self.title_lbl = ctk.CTkLabel(self.left_frame, text="Supplier Quote Extractor", font=ctk.CTkFont(size=20, weight="bold"))
        self.title_lbl.pack(pady=15, padx=10)

        # Settings box (API Key)
        self.settings_frame = ctk.CTkFrame(self.left_frame, fg_color="transparent")
        self.settings_frame.pack(pady=5, padx=10, fill="x")

        self.api_lbl = ctk.CTkLabel(self.settings_frame, text="Gemini API Key:", anchor="w")
        self.api_lbl.pack(fill="x")
        
        self.api_entry = ctk.CTkEntry(self.settings_frame, placeholder_text="AIzaSy...", show="*")
        self.api_entry.pack(fill="x", pady=2)
        if self.api_key:
            self.api_entry.insert(0, self.api_key)

        self.btn_save_api = ctk.CTkButton(self.settings_frame, text="Save & Test Key", command=self.save_and_test_key, height=26)
        self.btn_save_api.pack(pady=5, fill="x")

        # Folder selection / Direct Path Box
        self.folder_frame = ctk.CTkFrame(self.left_frame, fg_color="transparent")
        self.folder_frame.pack(pady=15, padx=10, fill="x")

        # Side-by-side buttons
        self.load_btn_frame = ctk.CTkFrame(self.folder_frame, fg_color="transparent")
        self.load_btn_frame.pack(fill="x", pady=(0, 5))

        self.btn_select_folder = ctk.CTkButton(self.load_btn_frame, text="Select Folder", command=self.select_folder, width=140)
        self.btn_select_folder.pack(side="left", fill="x", expand=True, padx=(0, 2))

        self.btn_select_files = ctk.CTkButton(self.load_btn_frame, text="+ Add Files", fg_color="#1f538d", hover_color="#153e6b", command=self.select_files, width=140)
        self.btn_select_files.pack(side="right", fill="x", expand=True, padx=(2, 0))

        self.folder_entry = ctk.CTkEntry(self.folder_frame, placeholder_text="Or paste folder path here...")
        self.folder_entry.pack(pady=2, fill="x")
        self.folder_entry.bind("<Return>", lambda event: self.on_path_entered())
        self.folder_entry.bind("<FocusOut>", lambda event: self.on_path_entered())

        # Files Queue List Views (Unsynced and Synced separated)
        self.unsynced_lbl = ctk.CTkLabel(self.left_frame, text="⏳ Unsynced Queue:", anchor="w", font=ctk.CTkFont(weight="bold"))
        self.unsynced_lbl.pack(fill="x", padx=10, pady=(5, 0))

        self.files_box_unsynced = tk.Listbox(self.left_frame, bg="#2b2b2b", fg="white", borderwidth=0, highlightthickness=0, selectbackground="#1f538d", selectforeground="white", font=("Segoe UI", 10), height=8)
        self.files_box_unsynced.pack(fill="both", expand=True, padx=10, pady=(2, 5))

        self.synced_lbl = ctk.CTkLabel(self.left_frame, text="✅ Synced Quotes:", anchor="w", font=ctk.CTkFont(weight="bold"))
        self.synced_lbl.pack(fill="x", padx=10, pady=(5, 0))

        self.files_box_synced = tk.Listbox(self.left_frame, bg="#2b2b2b", fg="white", borderwidth=0, highlightthickness=0, selectbackground="#1f538d", selectforeground="white", font=("Segoe UI", 10), height=8)
        self.files_box_synced.pack(fill="both", expand=True, padx=10, pady=(2, 5))

        # Control Buttons
        self.btn_start = ctk.CTkButton(self.left_frame, text="Start Extraction", state="disabled", command=self.start_extraction_thread)
        self.btn_start.pack(fill="x", padx=10, pady=5)

        self.btn_organize = ctk.CTkButton(self.left_frame, text="📁 Organize Files", fg_color="#6e4513", hover_color="#52320b", command=self.start_file_organizer_thread)
        self.btn_organize.pack(fill="x", padx=10, pady=5)

        self.progress_bar = ctk.CTkProgressBar(self.left_frame)
        self.progress_bar.pack(fill="x", padx=10, pady=5)
        self.progress_bar.set(0)

        # --- MIDDLE PANEL: CTkTabview Container ---
        self.right_frame = ctk.CTkFrame(self, corner_radius=0)
        self.right_frame.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        self.right_frame.grid_columnconfigure(0, weight=1)
        self.right_frame.grid_rowconfigure(0, weight=1)

        self.tabview = ctk.CTkTabview(self.right_frame)
        self.tabview.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        
        self.tabview.add("📊 Quotes Comparison")
        self.tabview.add("📇 Supplier Directory")
        self.tabview.add("📈 Visual Charts")
        self.tabview.add("💡 AI Sourcing Insights")
        self.tabview.add("🏆 Supplier Scorecard")
        self.tabview.add("📅 Sourcing Timeline")

        # --- TAB 1: Quotes Comparison Grid and Chatbot ---
        tab_comp = self.tabview.tab("📊 Quotes Comparison")
        tab_comp.grid_columnconfigure(0, weight=1)
        tab_comp.grid_rowconfigure(2, weight=1)

        # Row 0: Upper controls (Add, Edit, Delete, Paste)
        self.table_ctrl_frame = ctk.CTkFrame(tab_comp, fg_color="transparent")
        self.table_ctrl_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)

        self.table_lbl = ctk.CTkLabel(self.table_ctrl_frame, text="Extracted Quotes Comparison", font=ctk.CTkFont(size=18, weight="bold"))
        self.table_lbl.pack(side="left")

        self.btn_add_row = ctk.CTkButton(self.table_ctrl_frame, text="+ Add Row", width=90, command=self.add_empty_row)
        self.btn_add_row.pack(side="right", padx=5)

        self.btn_delete_row = ctk.CTkButton(self.table_ctrl_frame, text="- Delete Row", width=90, fg_color="#a83232", hover_color="#8c2626", command=self.delete_selected_row)
        self.btn_delete_row.pack(side="right", padx=5)

        self.btn_edit_row = ctk.CTkButton(self.table_ctrl_frame, text="✏ Edit Row", width=90, command=self.edit_selected_row)
        self.btn_edit_row.pack(side="right", padx=5)

        self.btn_attach_media = ctk.CTkButton(self.table_ctrl_frame, text="📎 Attach Media", width=100, command=self.attach_media_to_selected)
        self.btn_attach_media.pack(side="right", padx=5)

        self.btn_paste_chat = ctk.CTkButton(self.table_ctrl_frame, text="📋 Paste Chat", width=90, fg_color="#1f538d", command=self.open_paste_chat_window)
        self.btn_paste_chat.pack(side="right", padx=5)

        # Row 1: Search box & Clear All Database button
        self.search_frame = ctk.CTkFrame(tab_comp, fg_color="transparent")
        self.search_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        
        self.search_entry = ctk.CTkEntry(self.search_frame, placeholder_text="🔍 Type to filter by supplier, product, color, specs, etc...", height=28)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(5, 5), pady=5)
        self.search_entry.bind("<KeyRelease>", lambda event: self.filter_table())
        
        self.btn_clear_all = ctk.CTkButton(self.search_frame, text="🧹 Clear All Data", width=120, fg_color="#a83232", hover_color="#8c2626", command=self.clear_all_data)
        self.btn_clear_all.pack(side="right", padx=(5, 5), pady=5)

        # Row 2: Treeview styled table
        self.setup_table(tab_comp)

        # Row 4: AI Procurement Chatbot Panel
        self.chat_panel = ctk.CTkFrame(tab_comp, height=180)
        self.chat_panel.grid(row=4, column=0, sticky="ew", padx=10, pady=5)
        self.chat_panel.grid_propagate(False)
        self.chat_panel.grid_columnconfigure(0, weight=1)
        self.chat_panel.grid_rowconfigure(1, weight=1)

        self.chat_title = ctk.CTkLabel(self.chat_panel, text="💬 AI Procurement Assistant", font=ctk.CTkFont(size=13, weight="bold"))
        self.chat_title.grid(row=0, column=0, columnspan=2, padx=10, pady=3, sticky="w")

        self.chat_log = ctk.CTkTextbox(self.chat_panel, wrap="word", font=("Segoe UI", 9))
        self.chat_log.grid(row=1, column=0, columnspan=2, padx=10, pady=3, sticky="nsew")

        self.chat_input_frame = ctk.CTkFrame(self.chat_panel, fg_color="transparent")
        self.chat_input_frame.grid(row=2, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        self.chat_input_frame.grid_columnconfigure(0, weight=1)

        self.chat_entry = ctk.CTkEntry(self.chat_input_frame, placeholder_text="Ask AI assistant...", height=26)
        self.chat_entry.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.chat_entry.bind("<Return>", lambda event: self.send_chat_message())

        self.btn_chat_send = ctk.CTkButton(self.chat_input_frame, text="Send", width=60, height=26, command=self.send_chat_message)
        self.btn_chat_send.grid(row=0, column=1, sticky="e")

        # Row 5: Export Buttons & Currency dropdown
        self.export_frame = ctk.CTkFrame(tab_comp, fg_color="transparent")
        self.export_frame.grid(row=5, column=0, sticky="ew", padx=10, pady=10)

        self.currency_lbl = ctk.CTkLabel(self.export_frame, text="Currency:")
        self.currency_lbl.pack(side="left", padx=(10, 5))
        
        self.currency_cb = ctk.CTkComboBox(self.export_frame, values=["USD ($)", "CNY (¥)", "EUR (€)"], command=self.change_currency, width=120)
        self.currency_cb.pack(side="left", padx=5)

        self.btn_export_excel = ctk.CTkButton(self.export_frame, text="Export to Excel", fg_color="#1f7d44", hover_color="#15592e", command=self.export_to_excel)
        self.btn_export_excel.pack(side="right", padx=5)

        self.btn_export_csv = ctk.CTkButton(self.export_frame, text="Export to CSV", command=self.export_to_csv)
        self.btn_export_csv.pack(side="right", padx=5)



        # --- TAB 2: Supplier Directory ---
        tab_dir = self.tabview.tab("📇 Supplier Directory")
        tab_dir.grid_columnconfigure(0, weight=1)
        tab_dir.grid_rowconfigure(1, weight=1)

        self.dir_header_frame = ctk.CTkFrame(tab_dir, fg_color="transparent")
        self.dir_header_frame.grid(row=0, column=0, sticky="ew", padx=15, pady=10)

        self.dir_title = ctk.CTkLabel(self.dir_header_frame, text="Supplier Contact Directory", font=ctk.CTkFont(size=18, weight="bold"))
        self.dir_title.pack(side="left")

        self.btn_sync_dir = ctk.CTkButton(self.dir_header_frame, text="🔄 Sync from Quotes", width=140, fg_color="#1f538d", command=self.sync_suppliers_from_quotes)
        self.btn_sync_dir.pack(side="right", padx=5)

        self.btn_extract_contacts = ctk.CTkButton(self.dir_header_frame, text="🔍 Auto-Extract Contacts", width=160, fg_color="#1f7d44", hover_color="#15592e", command=self.start_contact_extraction_thread)
        self.btn_extract_contacts.pack(side="right", padx=5)

        self.directory_scroll_frame = ctk.CTkScrollableFrame(tab_dir, fg_color="#2b2b2b")
        self.directory_scroll_frame.grid(row=1, column=0, sticky="nsew", padx=15, pady=10)

        # --- TAB 3: Visual Price Comparison Charts ---
        tab_charts = self.tabview.tab("📈 Visual Charts")
        tab_charts.grid_columnconfigure(0, weight=1)
        tab_charts.grid_rowconfigure(1, weight=1)

        self.chart_ctrl_frame = ctk.CTkFrame(tab_charts, fg_color="transparent")
        self.chart_ctrl_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)

        self.chart_title_lbl = ctk.CTkLabel(self.chart_ctrl_frame, text="Price Comparison Dashboard", font=ctk.CTkFont(size=18, weight="bold"))
        self.chart_title_lbl.pack(side="left")

        self.chart_category_cb = ctk.CTkComboBox(self.chart_ctrl_frame, values=["All", "Cap", "Apron", "Sleeve", "Shoe", "Mask"], command=lambda choice: self.draw_chart(), width=150)
        self.chart_category_cb.pack(side="right", padx=10)

        self.chart_lbl = ctk.CTkLabel(self.chart_ctrl_frame, text="Filter Category:")
        self.chart_lbl.pack(side="right", padx=5)

        self.chart_display_frame = ctk.CTkFrame(tab_charts, fg_color="#2b2b2b")
        self.chart_display_frame.grid(row=1, column=0, sticky="nsew", padx=15, pady=10)

        # Setup Sourcing Insights tab
        self.setup_sourcing_insights_tab()

        # Setup Scorecard tab
        self.setup_scorecard_tab()

        # Setup Timeline tab
        self.setup_timeline_tab()

        # --- RIGHT PANEL 2: Document Preview Sidebar ---
        self.preview_frame = ctk.CTkFrame(self, width=360, corner_radius=0)
        self.preview_frame.grid(row=0, column=2, sticky="nsew", padx=10, pady=10)
        self.preview_frame.grid_columnconfigure(0, weight=1)
        self.preview_frame.grid_rowconfigure(3, weight=1)

        self.preview_title = ctk.CTkLabel(self.preview_frame, text="Document Preview", font=ctk.CTkFont(size=18, weight="bold"))
        self.preview_title.grid(row=0, column=0, pady=15, padx=10, sticky="ew")

        self.preview_filename_lbl = ctk.CTkLabel(self.preview_frame, text="Select a row to preview", wraplength=330, text_color="grey")
        self.preview_filename_lbl.grid(row=1, column=0, pady=5, padx=10, sticky="ew")

        # Horizontal Media Gallery Bar Frame
        self.preview_gallery_bar = ctk.CTkFrame(self.preview_frame, fg_color="transparent")
        self.preview_gallery_bar.grid(row=2, column=0, pady=5, padx=15, sticky="ew")

        # Container for preview media
        self.preview_display_frame = ctk.CTkFrame(self.preview_frame, fg_color="#2b2b2b")
        self.preview_display_frame.grid(row=3, column=0, sticky="nsew", padx=15, pady=10)
        self.preview_display_frame.grid_columnconfigure(0, weight=1)
        self.preview_display_frame.grid_rowconfigure(0, weight=1)

        self.preview_image_lbl = ctk.CTkLabel(self.preview_display_frame, text="No document selected")
        self.preview_image_lbl.pack(pady=120, padx=10)

        # Scrolling text box for text file rendering
        self.preview_text_box = ctk.CTkTextbox(self.preview_display_frame, wrap="word")

        # Metrics Overlay Frame inside preview_frame
        self.preview_metrics_frame = ctk.CTkFrame(self.preview_frame, fg_color="#2b2b2b")
        self.preview_metrics_frame.grid(row=4, column=0, sticky="ew", padx=15, pady=5)
        self.update_preview_metrics_overlay(None)

        # System open button
        self.btn_open_external = ctk.CTkButton(self.preview_frame, text="Open File Externally", state="disabled", command=self.open_file_externally)
        self.btn_open_external.grid(row=5, column=0, pady=10, padx=15, sticky="ew")

        # Load configurations & history
        self.load_config()
        self.load_chat_history_from_db()
        self.load_all_quotes_from_db()

    def get_validity_display(self, date_str):
        if not date_str or date_str.lower() in ["n/a", "null", "none", ""]:
            return "Unknown"
        import re
        match = re.search(r'\d{4}-\d{2}-\d{2}', date_str)
        if match:
            try:
                import datetime
                q_date = datetime.datetime.strptime(match.group(0), "%Y-%m-%d").date()
                today = datetime.date(2026, 8, 1) # Set current local date 2026-08-01
                if q_date < today:
                    return f"🔴 Expired ({match.group(0)})"
                else:
                    return f"🟢 Active ({match.group(0)})"
            except Exception:
                pass
        return date_str

    def get_risk_display(self, risk_str):
        if not risk_str or risk_str.lower() in ["n/a", "null", "none", ""]:
            return "Low Risk"
        r_lower = risk_str.lower()
        if "high" in r_lower or "prepayment" in r_lower or "expired" in r_lower:
            return f"🔴 {risk_str}"
        elif "medium" in r_lower or "warning" in r_lower or "long lead" in r_lower:
            return f"🟡 {risk_str}"
        else:
            return f"🟢 {risk_str}"

    def change_currency(self, choice):
        # Update column header with current currency name
        currency_choice = self.currency_cb.get()
        symbol = "$"
        if "CNY" in currency_choice:
            symbol = "¥"
        elif "EUR" in currency_choice:
            symbol = "€"
        self.tree.heading("price", text=f"Price ({symbol})")
        self.filter_table()
        self.draw_chart()

    def setup_table(self, parent):
        style = ttk.Style()
        style.theme_use("clam")
        
        # Style treeview to match dark mode
        style.configure("Treeview",
                        background="#2b2b2b",
                        foreground="white",
                        rowheight=25,
                        fieldbackground="#2b2b2b",
                        borderwidth=0,
                        font=("Segoe UI", 9))
        style.map("Treeview", background=[("selected", "#1f538d")])
        
        style.configure("Treeview.Heading",
                        background="#3c3c3c",
                        foreground="white",
                        borderwidth=1,
                        font=("Segoe UI", 9, "bold"))
        
        # Columns
        self.columns = ("id", "filename", "supplier", "product", "spec", "color", "elastic", "price", "unit", "moq", "packing", "term", "lead_time", "validity_date", "sourcing_risk")
        self.tree = ttk.Treeview(parent, columns=self.columns, show="headings", style="Treeview")
        
        # Setup column headers with commands for interactive sorting
        self.tree.heading("id", text="ID", command=lambda: self.sort_column("id", False))
        self.tree.heading("filename", text="Source File", command=lambda: self.sort_column("filename", False))
        self.tree.heading("supplier", text="Supplier", command=lambda: self.sort_column("supplier", False))
        self.tree.heading("product", text="Product", command=lambda: self.sort_column("product", False))
        self.tree.heading("spec", text="Specs", command=lambda: self.sort_column("spec", False))
        self.tree.heading("color", text="Color", command=lambda: self.sort_column("color", False))
        self.tree.heading("elastic", text="Elastic", command=lambda: self.sort_column("elastic", False))
        self.tree.heading("price", text="Price", command=lambda: self.sort_column("price", False)) # Dynamic header based on currency
        self.tree.heading("unit", text="Price Unit", command=lambda: self.sort_column("unit", False))
        self.tree.heading("moq", text="MOQ", command=lambda: self.sort_column("moq", False))
        self.tree.heading("packing", text="Packing", command=lambda: self.sort_column("packing", False))
        self.tree.heading("term", text="Price Term", command=lambda: self.sort_column("term", False))
        self.tree.heading("lead_time", text="Lead Time", command=lambda: self.sort_column("lead_time", False))
        self.tree.heading("validity_date", text="Validity", command=lambda: self.sort_column("validity_date", False))
        self.tree.heading("sourcing_risk", text="Risk Alerts", command=lambda: self.sort_column("sourcing_risk", False))

        # Column widths
        self.tree.column("id", width=40, anchor="center")
        self.tree.column("filename", width=120, anchor="w")
        self.tree.column("supplier", width=130, anchor="w")
        self.tree.column("product", width=100, anchor="w")
        self.tree.column("spec", width=110, anchor="w")
        self.tree.column("color", width=60, anchor="center")
        self.tree.column("elastic", width=60, anchor="center")
        self.tree.column("price", width=60, anchor="center")
        self.tree.column("unit", width=60, anchor="center")
        self.tree.column("moq", width=80, anchor="center")
        self.tree.column("packing", width=110, anchor="w")
        self.tree.column("term", width=80, anchor="center")
        self.tree.column("lead_time", width=70, anchor="center")
        self.tree.column("validity_date", width=95, anchor="center")
        self.tree.column("sourcing_risk", width=130, anchor="w")

        # Scrollbars
        ysb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(parent, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscroll=ysb.set, xscroll=xsb.set)

        # Place Treeview in row 2
        self.tree.grid(row=2, column=0, sticky="nsew", padx=10)
        ysb.grid(row=2, column=1, sticky="ns")
        xsb.grid(row=3, column=0, sticky="ew")

        # Bind events
        self.tree.bind("<Double-1>", lambda event: self.edit_selected_row())
        self.tree.bind("<<TreeviewSelect>>", lambda event: self.on_row_selected(event))

        # Best Deal highlight tag
        self.tree.tag_configure("best_deal", background="#1e4620", foreground="#a6ffa6")

    # --- Direct Path Entry Handler ---
    def on_path_entered(self):
        path = self.folder_entry.get().strip()
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        
        if path == self.selected_folder:
            self.folder_entry.configure(fg_color=None)
            return

        if path and os.path.exists(path) and os.path.isdir(path):
            self.selected_folder = path
            self.folder_entry.configure(fg_color=None)
            self.save_config()
            self.scan_folder()
        elif path:
            self.folder_entry.configure(fg_color="#5a1f1f")

    # --- Document Preview Event Handlers ---
    def on_row_selected(self, event):
        sel = self.tree.selection()
        if not sel:
            self.clear_preview()
            return
            
        item = sel[0]
        vals = self.tree.item(item, "values")
        
        # 1. Update Preview metrics overlay
        self.update_preview_metrics_overlay(vals)
        
        # 2. Update Gallery selector buttons
        row_id = vals[0]
        self.update_preview_gallery_bar(row_id, vals)
        
        filename = vals[1]
        self.show_preview(filename)

    def clear_preview(self):
        self.preview_filename_lbl.configure(text="Select a row to preview", text_color="grey")
        self.preview_image_lbl.pack_forget()
        self.preview_text_box.pack_forget()
        for widget in self.preview_display_frame.winfo_children():
            if widget not in [self.preview_image_lbl, self.preview_text_box]:
                widget.destroy()
        
        if hasattr(self, 'preview_gallery_bar'):
            for widget in self.preview_gallery_bar.winfo_children():
                widget.destroy()

        self.preview_image_lbl.configure(image=None, text="No document selected")
        self.preview_image_lbl.pack(pady=120, padx=10)
        self.btn_open_external.configure(state="disabled")
        self.current_preview_path = ""
        self.update_preview_metrics_overlay(None)

    def show_preview(self, filename):
        self.preview_filename_lbl.configure(text=filename, text_color="white")
        
        full_path = os.path.join(self.selected_folder, filename)
        
        if filename == "Manually Added" or "Chat (" in filename or not filename:
            self.show_preview_message("Text entry (no document preview)")
            self.btn_open_external.configure(state="disabled")
            return
            
        if not os.path.exists(full_path):
            self.show_preview_message(f"File not found in folder:\n{self.selected_folder}")
            self.btn_open_external.configure(state="disabled")
            return
            
        self.btn_open_external.configure(state="normal")
        self.current_preview_path = full_path
        
        ext = os.path.splitext(filename)[1].lower()
        
        try:
            self.preview_text_box.pack_forget()
            self.preview_image_lbl.pack_forget()
            
            if ext in {".png", ".jpg", ".jpeg"}:
                self.render_image_preview(full_path)
            elif ext == ".pdf":
                self.render_pdf_preview(full_path)
            elif ext == ".txt":
                self.render_text_preview(full_path)
            else:
                self.show_preview_message(f"Preview not supported for {ext} files.\nUse system button below to open.")
        except Exception as e:
            self.show_preview_message(f"Error loading preview:\n{e}")

    def render_image_preview(self, path):
        try:
            pil_img = Image.open(path)
            self.display_image_in_preview(pil_img)
        except Exception as e:
            self.show_preview_message(f"Failed to load image:\n{e}")

    def render_pdf_preview(self, path):
        try:
            import fitz
            doc = fitz.open(path)
            if len(doc) > 0:
                page = doc.load_page(0)
                pix = page.get_pixmap(dpi=120)
                pil_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                self.display_image_in_preview(pil_img)
            else:
                self.show_preview_message("PDF document has no pages")
        except ImportError:
            self.show_preview_message("Install PyMuPDF to view PDF previews")
        except Exception as e:
            self.show_preview_message(f"Failed to render PDF preview:\n{e}")

    def render_text_preview(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text_content = f.read(5000)
            self.preview_text_box.delete("1.0", tk.END)
            self.preview_text_box.insert("1.0", text_content)
            self.preview_text_box.pack(fill="both", expand=True, padx=5, pady=5)
        except Exception as e:
            self.show_preview_message(f"Failed to read text file:\n{e}")

    def display_image_in_preview(self, pil_img):
        width, height = pil_img.size
        max_w, max_h = 330, 480
        
        ratio = min(max_w / width, max_h / height)
        new_w = max(int(width * ratio), 1)
        new_h = max(int(height * ratio), 1)
        
        ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(new_w, new_h))
        self.preview_image_lbl.configure(image=ctk_img, text="")
        self.preview_image_lbl.image = ctk_img
        self.preview_image_lbl.pack(pady=10)

    def show_preview_message(self, message):
        self.preview_image_lbl.pack_forget()
        self.preview_text_box.pack_forget()
        self.preview_image_lbl.configure(image=None, text=message)
        self.preview_image_lbl.pack(pady=100)

    def open_file_externally(self):
        if self.current_preview_path:
            try:
                os.startfile(self.current_preview_path)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to open file: {e}")

    # --- Best Deals Group Key logic ---
    def get_group_key(self, row):
        prod = (row.get("product") or "").lower().strip()
        spec = (row.get("spec") or "").lower()
        
        category = "other"
        if "cap" in prod or "net" in prod or "bouffant" in prod:
            category = "cap"
        elif "apron" in prod:
            category = "apron"
        elif "sleeve" in prod:
            category = "sleeve"
        elif "shoe" in prod:
            category = "shoe_cover"
        elif "mask" in prod:
            category = "mask"
        else:
            category = prod
            
        import re
        numbers = re.findall(r'\d+', prod + " " + spec)
        size_key = numbers[0] if numbers else "default"
        
        return f"{category}_{size_key}"

    # --- Interactive Sorting logic ---
    def sort_column(self, col, reverse):
        def get_sort_val(row_dict):
            val = row_dict.get(col)
            if val is None:
                return ""
            
            if col == "price":
                try:
                    return float(val)
                except ValueError:
                    return 0.0
            if col == "id":
                try:
                    return int(val)
                except ValueError:
                    return 0
            return str(val).lower()

        self.extracted_data.sort(key=get_sort_val, reverse=reverse)
        self.filter_table()
        self.tree.heading(col, command=lambda: self.sort_column(col, not reverse))

    # --- Clear All Data reset ---
    def clear_all_data(self):
        ans = messagebox.askyesno(
            "Clear All Data",
            "⚠️ WARNING: This will permanently delete all quote data, processed files, chat history, and supplier directory records from the database.\n\nAre you sure you want to proceed?"
        )
        if ans:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("DELETE FROM extracted_quotes")
            c.execute("DELETE FROM processed_files")
            c.execute("DELETE FROM supplier_contacts")
            c.execute("DELETE FROM chat_history")
            conn.commit()
            conn.close()
            
            self.load_all_quotes_from_db()
            self.scan_folder()
            self.clear_preview()
            self.load_chat_history_from_db()
            
            messagebox.showinfo("Database Cleared", "Database reset successfully! All quotes and histories have been cleared.")

    # --- Live Search Filter rendering ---
    def filter_table(self):
        query = self.search_entry.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        
        # Get active currency conversion specs
        currency_choice = self.currency_cb.get()
        factor = 1.0
        symbol = "$"
        if "CNY" in currency_choice:
            factor = 7.25
            symbol = "¥"
        elif "EUR" in currency_choice:
            factor = 0.92
            symbol = "€"
        
        # Recalculate best price comparisons from in-memory quotes (baseline USD)
        groups = {}
        for r in self.extracted_data:
            price = r.get("price")
            try:
                price = float(price)
            except (ValueError, TypeError):
                continue
            if price <= 0:
                continue
            
            g_key = self.get_group_key(r)
            if g_key not in groups:
                groups[g_key] = []
            groups[g_key].append(price)

        best_prices = {}
        for g_key, prices in groups.items():
            if len(prices) > 1:
                best_prices[g_key] = min(prices)

        # Populate tree matching search query
        for row_data in self.extracted_data:
            match = False
            if not query:
                match = True
            else:
                fields = ["filename", "supplier", "product", "spec", "color", "elastic", "price", "unit", "moq", "packing", "term", "lead_time", "validity_date", "sourcing_risk"]
                for f in fields:
                    if query in str(row_data.get(f) or "").lower():
                        match = True
                        break
            
            if not match:
                continue

            try:
                price_val = float(row_data["price"])
                converted_price = price_val * factor
                price_display = f"{symbol}{converted_price:.5f}"
            except (ValueError, TypeError):
                price_display = str(row_data["price"])
            
            is_best_deal = False
            g_key = self.get_group_key(row_data)
            try:
                price_val = float(row_data["price"])
                if g_key in best_prices and abs(price_val - best_prices[g_key]) < 1e-7:
                    is_best_deal = True
            except (ValueError, TypeError):
                pass
                
            tags = ("best_deal",) if is_best_deal else ()
            
            self.tree.insert("", "end", values=(
                row_data["id"],
                row_data["filename"],
                row_data["supplier"],
                row_data["product"],
                row_data["spec"],
                row_data["color"],
                row_data["elastic"],
                price_display,
                row_data["unit"],
                row_data["moq"],
                row_data["packing"],
                row_data["term"],
                row_data["lead_time"],
                self.get_validity_display(row_data.get("validity_date")),
                self.get_risk_display(row_data.get("sourcing_risk"))
            ), tags=tags)

    # --- DB Retrieval ---
    def load_all_quotes_from_db(self):
        self.tree.delete(*self.tree.get_children())
        self.extracted_data = []

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            SELECT id, filename, supplier, product, spec, color, elastic, price, unit, moq, packing, term, lead_time, validity_date, sourcing_risk, attached_media 
            FROM extracted_quotes
        """)
        rows = c.fetchall()
        conn.close()

        for row in rows:
            row_data = {
                "id": row[0],
                "filename": row[1],
                "supplier": row[2],
                "product": row[3],
                "spec": row[4],
                "color": row[5],
                "elastic": row[6],
                "price": row[7],
                "unit": row[8],
                "moq": row[9],
                "packing": row[10],
                "term": row[11],
                "lead_time": row[12],
                "validity_date": row[13] if len(row) > 13 else "N/A",
                "sourcing_risk": row[14] if len(row) > 14 else "N/A",
                "attached_media": row[15] if len(row) > 15 else ""
            }
            self.extracted_data.append(row_data)

            if row_data["id"] >= self.quote_id_counter:
                self.quote_id_counter = row_data["id"] + 1

        self.filter_table()
        self.load_supplier_directory()
        self.update_chart_dropdown()
        self.draw_chart()
        self.update_sourcing_insights()
        self.update_scorecard_tab()
        self.update_timeline_tab()

    # --- Persistent Chat History logic ---
    def load_chat_history_from_db(self):
        self.chat_log.configure(state="normal")
        self.chat_log.delete("1.0", tk.END)
        self.chat_log.insert("1.0", "AI: Hello! Ask me anything about your current supplier quotes (e.g. comparing prices, payment terms, or drafting negotiation emails).\n")
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT sender, message FROM chat_history ORDER BY id ASC")
        rows = c.fetchall()
        conn.close()
        
        for sender, message in rows:
            self.chat_log.insert(tk.END, f"\n{sender}: {message}\n")
            
        self.chat_log.see(tk.END)
        self.chat_log.configure(state="disabled")

    def save_chat_message_to_db(self, sender, message):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO chat_history (sender, message) VALUES (?, ?)", (sender, message))
        conn.commit()
        conn.close()

    def sync_suppliers_from_quotes(self):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT DISTINCT supplier FROM extracted_quotes WHERE supplier IS NOT NULL AND supplier != 'Unknown'")
        suppliers = [row[0] for row in c.fetchall()]
        
        synced_count = 0
        for s in suppliers:
            c.execute("SELECT filename FROM extracted_quotes WHERE supplier = ? LIMIT 1", (s,))
            q_row = c.fetchone()
            filename = q_row[0] if q_row else "N/A"
            
            c.execute("SELECT COUNT(*) FROM supplier_contacts WHERE supplier = ?", (s,))
            if c.fetchone()[0] == 0:
                c.execute("INSERT INTO supplier_contacts (supplier, contact_info, source_file) VALUES (?, ?, ?)", 
                          (s, "Contact info placeholder. Click '✏ Edit Info' to fill manually, or process a new quote from this supplier to auto-extract details.", filename))
                synced_count += 1
            else:
                c.execute("UPDATE supplier_contacts SET source_file = ? WHERE supplier = ? AND (source_file IS NULL OR source_file = '')", (filename, s))
                
        conn.commit()
        conn.close()
        
        self.load_supplier_directory()
        messagebox.showinfo("Sync Complete", f"Successfully synced supplier directory!\nAdded {synced_count} new supplier cards.")

    def start_contact_extraction_thread(self):
        if not self.api_key:
            messagebox.showerror("API Key Missing", "Please enter and save your Gemini API Key first.")
            return
        if not self.selected_folder:
            messagebox.showerror("Folder Missing", "Please select a folder containing the quotes first.")
            return
            
        self.btn_sync_dir.configure(state="disabled")
        self.btn_extract_contacts.configure(state="disabled", text="Extracting...")
        
        threading.Thread(target=self.run_contact_extraction, daemon=True).start()

    def run_contact_extraction(self):
        try:
            prompt = """
            You are an expert contact card extractor. Look at this document or image and extract:
            1. The supplier company name.
            2. All contact details (phone numbers, email addresses, WhatsApp numbers, websites, address) formatted as a clean, concise, multi-line text block.
            
            Return the results strictly matching this JSON schema:
            {
              "supplier_name": "Name of the supplier company",
              "contact_info": "Phone: ...\\nEmail: ...\\nWhatsApp: ...\\nWebsite: ..."
            }
            Output ONLY raw JSON. No markdown backticks.
            """
            
            # Read files in selected folder
            valid_exts = {".pdf", ".png", ".jpg", ".jpeg", ".txt"}
            files_to_process = []
            if os.path.exists(self.selected_folder):
                for file in os.listdir(self.selected_folder):
                    ext = os.path.splitext(file)[1].lower()
                    if ext in valid_exts:
                        files_to_process.append(os.path.join(self.selected_folder, file))
                        
            if not files_to_process:
                self.after(0, lambda: messagebox.showinfo("No Files", "No printable quote files found in the selected folder."))
                self.after(0, self.finish_contact_extraction)
                return
                
            for path in files_to_process:
                try:
                    import mimetypes
                    mime_type, _ = mimetypes.guess_type(path)
                    if not mime_type:
                        ext = os.path.splitext(path)[1].lower()
                        if ext == ".pdf":
                            mime_type = "application/pdf"
                        elif ext == ".png":
                            mime_type = "image/png"
                        elif ext in {".jpg", ".jpeg"}:
                            mime_type = "image/jpeg"
                        elif ext == ".txt":
                            mime_type = "text/plain"
                        else:
                            mime_type = "application/octet-stream"

                    with open(path, "rb") as f:
                        file_bytes = f.read()

                    response_text = self.generate_with_fallback(
                        [{"mime_type": mime_type, "data": file_bytes}],
                        prompt
                    )
                    
                    result = json.loads(response_text)
                    supplier = result.get("supplier_name") or "Unknown"
                    contact_info = result.get("contact_info") or "N/A"
                    
                    if supplier != "Unknown" and contact_info != "N/A":
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        c.execute("INSERT OR REPLACE INTO supplier_contacts (supplier, contact_info, source_file) VALUES (?, ?, ?)", (supplier, contact_info, os.path.basename(path)))
                        conn.commit()
                        conn.close()
                        
                        # Refresh UI live
                        self.after(0, self.load_supplier_directory)
                        
                    import time
                    time.sleep(2.0)
                    
                except Exception as e:
                    print(f"Error extracting contact from {os.path.basename(path)}: {e}")
                    
            self.after(0, lambda: messagebox.showinfo("Success", "Finished extracting supplier contacts from files!"))
        except Exception as e:
            self.after(0, lambda e_err=e: messagebox.showerror("Error", f"An error occurred: {e_err}"))
        finally:
            self.after(0, self.finish_contact_extraction)

    def finish_contact_extraction(self):
        self.btn_sync_dir.configure(state="normal")
        self.btn_extract_contacts.configure(state="normal", text="🔍 Auto-Extract Contacts")

    def start_one_time_risk_extraction_thread(self):
        if not self.api_key:
            messagebox.showerror("API Key Missing", "Please enter and save your Gemini API Key first.")
            return
        if not self.selected_folder:
            messagebox.showerror("Folder Missing", "Please select a folder containing the quotes first.")
            return
            
        self.btn_start.configure(state="disabled")
        
        threading.Thread(target=self.run_one_time_risk_extraction, daemon=True).start()

    def run_one_time_risk_extraction(self):
        try:
            prompt = """
            You are an expert quote validity and risk auditor. Look at this quotation document and extract:
            1. The quotation validity date or expiration date of this quote. Return in YYYY-MM-DD format if possible.
               If not explicitly mentioned, look for quote date and validity period (e.g. quote dated 2026-05-10, valid for 30 days -> return 2026-06-09). If not found, return "N/A".
            2. Sourcing risk details (e.g., payment term risk, extremely long lead times, shipping port risks, or lack of certification details). Keep it short (under 15 words).
            
            Return the results strictly matching this JSON schema:
            {
              "validity_date": "YYYY-MM-DD or N/A",
              "sourcing_risk": "Low risk / Medium risk: ... / High risk: ..."
            }
            Output ONLY raw JSON. No markdown backticks.
            """
            
            # Get list of unique files already processed in quotes table
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT DISTINCT filename FROM extracted_quotes WHERE filename != 'Manually Added' AND filename NOT LIKE 'Chat (%'")
            files = [row[0] for row in c.fetchall()]
            conn.close()
            
            if not files:
                self.after(0, lambda: messagebox.showinfo("No Records", "No processed quote files found in the database to audit."))
                self.after(0, self.finish_one_time_risk_extraction)
                return
                
            audited_count = 0
            for filename in files:
                full_path = os.path.join(self.selected_folder, filename)
                if not os.path.exists(full_path):
                    continue
                    
                try:
                    import mimetypes
                    mime_type, _ = mimetypes.guess_type(full_path)
                    if not mime_type:
                        ext = os.path.splitext(full_path)[1].lower()
                        if ext == ".pdf":
                            mime_type = "application/pdf"
                        elif ext == ".png":
                            mime_type = "image/png"
                        elif ext in {".jpg", ".jpeg"}:
                            mime_type = "image/jpeg"
                        elif ext == ".txt":
                            mime_type = "text/plain"
                        else:
                            mime_type = "application/octet-stream"

                    with open(full_path, "rb") as f:
                        file_bytes = f.read()

                    response_text = self.generate_with_fallback(
                        [{"mime_type": mime_type, "data": file_bytes}],
                        prompt
                    )
                    
                    result = json.loads(response_text)
                    validity_date = result.get("validity_date") or "N/A"
                    sourcing_risk = result.get("sourcing_risk") or "N/A"
                    
                    conn = sqlite3.connect(DB_FILE)
                    c = conn.cursor()
                    c.execute("""
                        UPDATE extracted_quotes 
                        SET validity_date = ?, sourcing_risk = ?
                        WHERE filename = ?
                    """, (validity_date, sourcing_risk, filename))
                    conn.commit()
                    conn.close()
                    
                    audited_count += 1
                    
                    import time
                    time.sleep(4.0)
                    
                except Exception as e:
                    print(f"Error auditing {filename}: {e}")
                    
            self.after(0, lambda count=audited_count: messagebox.showinfo("Audit Complete", f"Successfully audited existing files!\nUpdated validity dates and risk alerts for {count} quotes in the grid without changing any other price or spec details."))
            
        except Exception as e:
            self.after(0, lambda e_err=e: messagebox.showerror("Error", f"Audit failed: {e_err}"))
        finally:
            self.after(0, self.finish_one_time_risk_extraction)

    def finish_one_time_risk_extraction(self):
        self.btn_start.configure(state="normal")
        self.load_all_quotes_from_db()

    # --- Supplier Directory rendering logic ---
    def load_supplier_directory(self):
        for widget in self.directory_scroll_frame.winfo_children():
            widget.destroy()
            
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT supplier, contact_info, source_file FROM supplier_contacts")
        rows = c.fetchall()
        conn.close()
        
        if not rows:
            lbl = ctk.CTkLabel(self.directory_scroll_frame, text="No suppliers found in directory.\nProcess some files or paste chats to populate cards!", text_color="grey")
            lbl.pack(pady=50)
            return
            
        for supplier, contact_info, source_file in rows:
            card = ctk.CTkFrame(self.directory_scroll_frame, corner_radius=8, border_width=1, border_color="#3c3c3c")
            card.pack(fill="x", padx=10, pady=5)
            
            title = ctk.CTkLabel(card, text=supplier, font=ctk.CTkFont(size=14, weight="bold"))
            title.pack(anchor="w", padx=15, pady=(10, 2))
            
            info_lbl = ctk.CTkLabel(card, text=contact_info, text_color="lightgrey", justify="left")
            info_lbl.pack(anchor="w", padx=15, pady=(2, 2))
            
            src_val = source_file or "N/A"
            src_lbl = ctk.CTkLabel(card, text=f"Source: {src_val}", text_color="grey", font=ctk.CTkFont(size=10, slant="italic"))
            src_lbl.pack(anchor="w", padx=15, pady=(2, 10))
            
            btn_frame = ctk.CTkFrame(card, fg_color="transparent")
            btn_frame.pack(fill="x", padx=15, pady=(0, 10), anchor="e")
            
            email = self.extract_email_from_text(contact_info)
            phone = self.extract_phone_from_text(contact_info)
            
            if email:
                btn_email = ctk.CTkButton(btn_frame, text="📧 Email", width=70, height=22, command=lambda e=email: self.open_email(e))
                btn_email.pack(side="left", padx=2)
            if phone:
                btn_wa = ctk.CTkButton(btn_frame, text="💬 WhatsApp", width=80, height=22, fg_color="#1f7d44", hover_color="#15592e", command=lambda p=phone: self.open_whatsapp(p))
                btn_wa.pack(side="left", padx=2)
                
            btn_edit = ctk.CTkButton(btn_frame, text="✏ Edit Info", width=70, height=22, command=lambda s=supplier, c_info=contact_info: self.edit_supplier_contact(s, c_info))
            btn_edit.pack(side="right", padx=2)

    def extract_email_from_text(self, text):
        import re
        match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text)
        return match.group(0) if match else None

    def extract_phone_from_text(self, text):
        import re
        cleaned_text = re.sub(r'[^\w\s\+]', '', text)
        match = re.search(r'\+?\d{8,15}', cleaned_text.replace(" ", ""))
        return match.group(0) if match else None

    def open_email(self, email):
        try:
            webbrowser.open(f"mailto:{email}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not open mail client:\n{e}")

    def open_whatsapp(self, phone):
        cleaned = phone.replace("+", "").replace(" ", "").strip()
        try:
            webbrowser.open(f"https://wa.me/{cleaned}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not open web browser:\n{e}")

    def edit_supplier_contact(self, supplier, current_info):
        edit_win = ctk.CTkToplevel(self)
        edit_win.title(f"Edit Info: {supplier}")
        edit_win.geometry("400x250")
        edit_win.resizable(False, False)
        edit_win.attributes("-topmost", True)
        
        lbl = ctk.CTkLabel(edit_win, text=f"Edit Contact Details for {supplier}:", font=ctk.CTkFont(size=13, weight="bold"))
        lbl.pack(pady=10, padx=15, anchor="w")
        
        textbox = ctk.CTkTextbox(edit_win, height=100)
        textbox.pack(fill="both", expand=True, padx=15, pady=5)
        textbox.insert("1.0", current_info)
        
        def save_contact():
            new_info = textbox.get("1.0", tk.END).strip()
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO supplier_contacts (supplier, contact_info) VALUES (?, ?)", (supplier, new_info))
            conn.commit()
            conn.close()
            
            self.load_supplier_directory()
            edit_win.destroy()
            
        btn_save = ctk.CTkButton(edit_win, text="Save Contact Details", fg_color="#1f7d44", hover_color="#15592e", command=save_contact)
        btn_save.pack(pady=15, padx=15, fill="x")

    # --- Matplotlib Visual Charts Dashboard ---
    def clean_supplier_name(self, name):
        if not name:
            return "Unknown"
        name = name.strip().title()
        # Clean common corporate suffix tokens
        for sfx in ["Nonwoven", "Protective", "Products", "Co.", "Ltd", "Co.,Ltd", "Corporation", "Factory", "Manufacturing", "Industry", "Limited", "Ltd.", "Co", "Plastic"]:
            name = name.replace(sfx.title(), "")
        words = name.split()
        return " ".join(words[:2]).strip()

    def clean_product_name(self, product):
        if not product:
            return "Product"
        p = product.split("(")[0].strip()
        p = p.split("-")[0].strip()
        words = p.split()
        res = " ".join(words[:3]).strip()
        if len(res) > 16:
            res = res[:14] + "..."
        return res

    def update_chart_dropdown(self):
        products = set()
        for r in self.extracted_data:
            prod = (r.get("product") or "").strip().title()
            if prod:
                products.add(prod)
                
        sorted_prods = ["All"] + sorted(list(products))
        
        # 1. Update charts dropdown
        current_chart = self.chart_category_cb.get()
        self.chart_category_cb.configure(values=sorted_prods)
        if current_chart in sorted_prods:
            self.chart_category_cb.set(current_chart)
        else:
            self.chart_category_cb.set("All")
            
        # 2. Update insights dropdown
        if hasattr(self, 'insights_category_cb'):
            current_insight = self.insights_category_cb.get()
            self.insights_category_cb.configure(values=sorted_prods)
            if current_insight in sorted_prods:
                self.insights_category_cb.set(current_insight)
            else:
                self.insights_category_cb.set("All")

        # 3. Update scorecard dropdown
        if hasattr(self, 'scorecard_category_cb'):
            current_scorecard = self.scorecard_category_cb.get()
            self.scorecard_category_cb.configure(values=sorted_prods)
            if current_scorecard in sorted_prods:
                self.scorecard_category_cb.set(current_scorecard)
            else:
                self.scorecard_category_cb.set("All")

    def draw_chart(self):
        for widget in self.chart_display_frame.winfo_children():
            widget.destroy()
            
        category = self.chart_category_cb.get().lower()
        
        # Get active currency conversion specs for visual charts
        currency_choice = self.currency_cb.get()
        factor = 1.0
        symbol = "$"
        currency_name = "USD"
        if "CNY" in currency_choice:
            factor = 7.25
            symbol = "¥"
            currency_name = "CNY"
        elif "EUR" in currency_choice:
            factor = 0.92
            symbol = "€"
            currency_name = "EUR"

        data = []
        for r in self.extracted_data:
            prod_name = (r.get("product") or "").strip().lower()
            
            if category != "all" and prod_name != category:
                continue
                
            price = r.get("price")
            try:
                price = float(price)
            except (ValueError, TypeError):
                continue
                
            if price > 0:
                data.append({
                    "supplier": r.get("supplier", "Unknown"),
                    "product": r.get("product", "Product"),
                    "price": price * factor
                })
                
        if not data:
            lbl = ctk.CTkLabel(self.chart_display_frame, text="No price data available for category to display charts.", text_color="grey")
            lbl.pack(pady=100)
            return
            
        df = pd.DataFrame(data)
        # Sort and select Top 12 cheapest quotes to prevent overcrowding
        df = df.sort_values("price", ascending=True)
        if len(df) > 12:
            df = df.head(12)

        df["label"] = df["supplier"].apply(self.clean_supplier_name) + "\n(" + df["product"].apply(self.clean_product_name) + ")"
        
        fig, ax = plt.subplots(figsize=(8, 4.5), dpi=100)
        fig.patch.set_facecolor('#2b2b2b')
        ax.set_facecolor('#2b2b2b')
        
        bars = ax.bar(df["label"], df["price"], color="#1f538d", edgecolor="#1a4473")
        
        ax.set_title(f"Price Comparison: {category.capitalize()}", color="white", fontsize=12, pad=15)
        ax.set_ylabel(f"Unit Price ({currency_name})", color="white", fontsize=10)
        ax.tick_params(colors="white", labelsize=8)
        
        plt.xticks(rotation=15, ha="right")
        ax.yaxis.grid(True, linestyle="--", alpha=0.3, color="white")
        ax.set_axisbelow(True)
        
        for spine in ax.spines.values():
            spine.set_edgecolor('#3c3c3c')
            
        for bar in bars:
            yval = bar.get_height()
            ax.text(bar.get_x() + bar.get_width()/2.0, yval, f"{symbol}{yval:.5f}", ha='center', va='bottom', color='white', fontsize=7.5)
            
        plt.tight_layout()
        
        canvas = FigureCanvasTkAgg(fig, master=self.chart_display_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        plt.close(fig)

    # --- Config Management ---
    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    config = json.load(f)
                    self.api_key = config.get("api_key", "")
                    self.selected_folder = config.get("last_folder", "")
            except Exception:
                pass
        
        if self.selected_folder:
            self.folder_entry.delete(0, tk.END)
            self.folder_entry.insert(0, self.selected_folder)
            if os.path.exists(self.selected_folder):
                self.scan_folder()

    def save_config(self):
        config = {
            "api_key": self.api_key,
            "last_folder": self.selected_folder
        }
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f)
        except Exception:
            pass

    def save_and_test_key(self):
        key = self.api_entry.get().strip()
        if not key:
            messagebox.showerror("Error", "Please enter a valid API Key")
            return
        
        self.api_key = key
        self.save_config()

        threading.Thread(target=self.async_test_key, daemon=True).start()

    def async_test_key(self):
        try:
            self.generate_with_fallback([], "Ping", json_response=False)
            self.api_entry.configure(fg_color="#1f5a34")
            messagebox.showinfo("Success", "Gemini API Key is valid and working!")
        except Exception as e:
            self.api_entry.configure(fg_color="#5a1f1f")
            messagebox.showerror("Error", f"Failed to connect to Gemini API:\n{e}")

    # --- Folder Logic ---
    def select_folder(self):
        folder = filedialog.askdirectory(initialdir=self.selected_folder)
        if folder:
            self.selected_folder = folder
            self.folder_entry.delete(0, tk.END)
            self.folder_entry.insert(0, folder)
            self.folder_entry.configure(fg_color=None)
            self.save_config()
            self.scan_folder()

    def scan_folder(self):
        self.files_box_unsynced.delete(0, tk.END)
        self.files_box_synced.delete(0, tk.END)
        self.files_list = []
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT filename FROM processed_files")
        processed = {row[0] for row in c.fetchall()}
        conn.close()
        
        valid_exts = {".pdf", ".png", ".jpg", ".jpeg", ".txt", ".xlsx", ".xls"}
        if os.path.exists(self.selected_folder):
            for file in os.listdir(self.selected_folder):
                ext = os.path.splitext(file)[1].lower()
                if ext in valid_exts:
                    full_path = os.path.join(self.selected_folder, file)
                    self.files_list.append((file, full_path))
                    
                    if file in processed:
                        self.files_box_synced.insert(tk.END, f"✅ {file}")
                    else:
                        self.files_box_unsynced.insert(tk.END, f"⏳ {file}")
            
            # Enable Start button if there are any unsynced files
            has_unsynced = any(name not in processed for (name, path) in self.files_list)
            if has_unsynced:
                self.btn_start.configure(state="normal")
            else:
                self.btn_start.configure(state="disabled")

    # --- Extraction Engine ---
    def animate_spinner(self):
        if self.is_extracting:
            spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            current_char = spinners[self.spinner_idx % len(spinners)]
            self.spinner_idx += 1
            
            self.btn_start.configure(text=f"Extracting {current_char}")
            self.after(100, self.animate_spinner)

    def start_extraction_thread(self):
        if not self.api_key:
            messagebox.showerror("API Key Missing", "Please enter and save your Gemini API Key before starting.")
            return
        self.btn_start.configure(state="disabled")
        self.btn_select_folder.configure(state="disabled")
        
        self.is_extracting = True
        self.spinner_idx = 0
        self.animate_spinner()
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()
        
        threading.Thread(target=self.run_extraction, daemon=True).start()

    def save_extracted_data_to_db(self, filename, data):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        try:
            c.execute("INSERT OR REPLACE INTO processed_files (filename) VALUES (?)", (filename,))
        except Exception as e:
            print(f"Error marking file in DB: {e}")

        supplier = data.get("supplier_name") or "Unknown"
        term = data.get("price_term") or "N/A"
        payment = data.get("payment_terms") or "N/A"
        lead_time = data.get("delivery_lead_time") or "N/A"
        
        validity_date = data.get("validity_date") or "N/A"
        sourcing_risk = data.get("sourcing_risk") or "N/A"
        
        # Save contact details to db
        contact_info = data.get("contact_info") or "N/A"
        if supplier != "Unknown":
            c.execute("INSERT OR REPLACE INTO supplier_contacts (supplier, contact_info, source_file) VALUES (?, ?, ?)", (supplier, contact_info, filename))

        quotes = data.get("quotes") or []
        for quote in quotes:
            c.execute("""
                INSERT INTO extracted_quotes (filename, supplier, product, spec, color, elastic, price, unit, moq, packing, term, lead_time, validity_date, sourcing_risk)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                filename,
                supplier,
                quote.get("product_name") or "N/A",
                quote.get("specifications") or "N/A",
                quote.get("color") or "N/A",
                quote.get("elastic_type") or "N/A",
                quote.get("unit_price_usd") or 0.0,
                quote.get("price_unit") or "piece",
                quote.get("moq") or "N/A",
                quote.get("packing_details") or "N/A",
                term,
                lead_time,
                validity_date,
                sourcing_risk
            ))
        conn.commit()
        conn.close()

    def run_extraction(self):
        prompt = """
        You are an expert procurement assistant. Extract all quotation details from the provided document or image.
        
        Return the results strictly matching this JSON schema:
        {
          "supplier_name": "Name of the supplier (e.g. Hubei Ruichen, Xiantao Topmed, etc.)",
          "contact_info": "email, phone number, website if visible",
          "price_term": "FOB Wuhan, FOB Shanghai, EXW, etc. (default to FOB if not mentioned but specified near price)",
          "payment_terms": "e.g. 30% deposit, 70% balance",
          "delivery_lead_time": "e.g. 30 days",
          "validity_date": "YYYY-MM-DD format (extract validity or expiration date of this quote. E.g. Valid until 2026-10-31 or quote date + validity duration)",
          "sourcing_risk": "e.g. 'Low: standard terms', 'Medium: long lead time (45 days)', 'High: 50% upfront prepayment required', etc.",
          "quotes": [
            {
              "product_name": "Surgical Cap, Clip Cap, Apron, Poncho, etc.",
              "specifications": "size, weight, gsm, elastic specs (e.g. 10g, 21 inch)",
              "color": "White, Blue, Black, etc.",
              "elastic_type": "Single, Double, or None",
              "unit_price_usd": 0.0059, // float unit price. If quoted per 1000pcs or bag, calculate unit price (divide by quantity). If quoted in CNY/RMB, convert to USD (use 1 USD = 7.2 CNY) and note in specifications.
              "price_unit": "piece", // strictly piece
              "packing_details": "e.g. 100pcs/bag, 1000pcs/carton",
              "moq": "e.g. 100,000 pcs"
            }
          ]
        }

        Make sure:
        - If a value is missing or not applicable, return null or empty string.
        - Calculate the single piece unit price in USD even if they quote per bag, carton, or per 1000pcs.
        - Output ONLY raw JSON. No markdown backticks.
        """

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT filename FROM processed_files")
        processed = {row[0] for row in c.fetchall()}
        conn.close()

        unsynced_files = [(name, path) for (name, path) in self.files_list if name not in processed]
        if not unsynced_files:
            self.is_extracting = False
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            self.progress_bar.set(1.0)
            self.btn_start.configure(state="disabled", text="Start Extraction")
            messagebox.showinfo("Extraction Info", "All files in the queue are already synced!")
            return

        total_files = len(unsynced_files)
        for i, (name, path) in enumerate(unsynced_files):
            idx = -1
            for box_idx in range(self.files_box_unsynced.size()):
                if name in self.files_box_unsynced.get(box_idx):
                    idx = box_idx
                    break
            
            if idx != -1:
                self.files_box_unsynced.delete(idx)
                self.files_box_unsynced.insert(idx, f"🔄 {name}")
                
            self.progress_bar.set(i / total_files)
            
            success = False
            retries = 3
            while not success and retries > 0:
                try:
                    ext = os.path.splitext(path)[1].lower()
                    if ext in {".xlsx", ".xls"}:
                        import pandas as pd
                        df = pd.read_excel(path)
                        csv_text = df.to_csv(index=False)
                        content_list = [f"EXCEL SPREADSHEET CONTENT (CSV format):\n\n{csv_text}"]
                    else:
                        import mimetypes
                        mime_type, _ = mimetypes.guess_type(path)
                        if not mime_type:
                            if ext == ".pdf":
                                mime_type = "application/pdf"
                            elif ext == ".png":
                                mime_type = "image/png"
                            elif ext in {".jpg", ".jpeg"}:
                                mime_type = "image/jpeg"
                            elif ext == ".txt":
                                mime_type = "text/plain"
                            else:
                                mime_type = "application/octet-stream"

                        with open(path, "rb") as f:
                            file_bytes = f.read()
                        content_list = [{"mime_type": mime_type, "data": file_bytes}]

                    response_text = self.generate_with_fallback(
                        content_list,
                        prompt
                    )
                    
                    result = json.loads(response_text)
                    self.save_extracted_data_to_db(name, result)
                    
                    self.load_all_quotes_from_db()
                    
                    # Move UI item
                    self.after(0, lambda n=name: self.move_file_to_synced(n))
                    success = True
                    
                    import time
                    time.sleep(6.0)
                    
                except Exception as e:
                    if "ResourceExhausted" in str(type(e)) or "429" in str(e):
                        print(f"Rate limit hit for {name}. Waiting 15s before retry... ({retries} left)")
                        for box_idx in range(self.files_box_unsynced.size()):
                            if name in self.files_box_unsynced.get(box_idx):
                                self.files_box_unsynced.delete(box_idx)
                                self.files_box_unsynced.insert(box_idx, f"⏳ {name} (Rate Limit Wait)")
                                break
                        import time
                        time.sleep(15.0)
                        retries -= 1
                    else:
                        for box_idx in range(self.files_box_unsynced.size()):
                            if name in self.files_box_unsynced.get(box_idx):
                                self.files_box_unsynced.delete(box_idx)
                                self.files_box_unsynced.insert(box_idx, f"❌ {name} (Error)")
                                break
                        print(f"Error processing {name}: {e}")
                        break
            
            if not success and retries == 0:
                for box_idx in range(self.files_box_unsynced.size()):
                    if name in self.files_box_unsynced.get(box_idx):
                        self.files_box_unsynced.delete(box_idx)
                        self.files_box_unsynced.insert(box_idx, f"❌ {name} (Limit Exceeded)")
                        break
                
        self.is_extracting = False
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(1.0)
        self.btn_start.configure(state="disabled", text="Start Extraction")
        self.btn_select_folder.configure(state="normal")
        messagebox.showinfo("Processing Completed", "Finished processing all files in the queue!")

    # --- Chat Extraction Engine ---
    def open_paste_chat_window(self):
        if not self.api_key:
            messagebox.showerror("API Key Missing", "Please enter and save your Gemini API Key first.")
            return

        chat_win = ctk.CTkToplevel(self)
        chat_win.title("Extract Quotes from Chat Text")
        chat_win.geometry("500x550")
        chat_win.resizable(False, False)
        chat_win.attributes("-topmost", True)

        lbl = ctk.CTkLabel(chat_win, text="Paste WhatsApp or Made-in-China Chat Text:", font=ctk.CTkFont(size=14, weight="bold"))
        lbl.pack(pady=10, padx=15, anchor="w")

        textbox = ctk.CTkTextbox(chat_win, height=350)
        textbox.pack(fill="both", expand=True, padx=15, pady=5)
        textbox.insert("1.0", "Paste chat content here...\n\nExample:\nSupplier: We can offer Surgical Cap 21 inch double elastic blue color at $0.0055/pc FOB Wuhan.\nBuyer: What is the MOQ?\nSupplier: 100,000pcs.")

        self.chat_is_extracting = False
        self.chat_spinner_idx = 0

        def animate_chat_spinner(btn):
            if self.chat_is_extracting:
                spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
                char = spinners[self.chat_spinner_idx % len(spinners)]
                self.chat_spinner_idx += 1
                btn.configure(text=f"Extracting {char}")
                chat_win.after(100, lambda: animate_chat_spinner(btn))

        def process_chat():
            raw_text = textbox.get("1.0", tk.END).strip()
            if not raw_text or raw_text.startswith("Paste chat content"):
                messagebox.showwarning("Empty Content", "Please paste chat text before extracting.", parent=chat_win)
                return

            btn_extract.configure(state="disabled")
            self.chat_is_extracting = True
            self.chat_spinner_idx = 0
            animate_chat_spinner(btn_extract)

            threading.Thread(target=self.run_chat_extraction, args=(raw_text, chat_win), daemon=True).start()

        btn_extract = ctk.CTkButton(chat_win, text="Extract & Add Quotes", fg_color="#1f7d44", hover_color="#15592e", command=process_chat)
        btn_extract.pack(pady=15, padx=15, fill="x")

    def run_chat_extraction(self, text, window):
        try:
            prompt = """
            You are an expert procurement assistant. Extract all quotation details from the provided raw chat text conversation (from WhatsApp or Made-in-China).
            Analyze the chat and extract the final or best quoted prices, specifications, and terms.
            
            Return the results strictly matching this JSON schema:
            {
              "supplier_name": "Name of the supplier if mentioned in the chat text (otherwise set as Unknown)",
              "contact_info": "email, phone number, website if visible in the chat text",
              "price_term": "FOB Wuhan, FOB Shanghai, EXW, etc. (default to FOB if not mentioned but specified near price)",
              "payment_terms": "e.g. 30% deposit, 70% balance",
              "delivery_lead_time": "e.g. 30 days",
              "validity_date": "YYYY-MM-DD format (extract validity or expiration date of this quote if mentioned, otherwise null)",
              "sourcing_risk": "e.g. 'Low: standard terms', 'Medium: long lead time', 'High: 50% upfront prepayment required', etc.",
              "quotes": [
                {
                  "product_name": "Surgical Cap, Clip Cap, Apron, Poncho, etc.",
                  "specifications": "size, weight, gsm, elastic specs (e.g. 10g, 21 inch)",
                  "color": "White, Blue, Black, etc.",
                  "elastic_type": "Single, Double, or None",
                  "unit_price_usd": 0.0059, // float unit price. If quoted per 1000pcs or bag, calculate unit price (divide by quantity). If quoted in CNY/RMB, convert to USD (use 1 USD = 7.2 CNY).
                  "price_unit": "piece", // strictly piece
                  "packing_details": "e.g. 100pcs/bag, 1000pcs/carton",
                  "moq": "e.g. 100,000 pcs"
                }
              ]
            }
            Output ONLY raw JSON. No markdown backticks.
            """

            response_text = self.generate_with_fallback(
                [f"CHAT CONVERSATION TEXT:\n\n{text}"],
                prompt
            )

            result = json.loads(response_text)

            import datetime
            now_str = datetime.datetime.now().strftime("%H:%M:%S")
            source_name = f"Chat ({now_str})"

            self.save_extracted_data_to_db(source_name, result)
            self.after(0, lambda: self.finish_chat_extraction(source_name, window))

        except Exception as e:
            self.chat_is_extracting = False
            self.after(0, lambda: messagebox.showerror("Extraction Error", f"Failed to extract chat quotes:\n{e}", parent=window))
            self.after(0, lambda: window.destroy())

    def finish_chat_extraction(self, source_name, window):
        self.chat_is_extracting = False
        self.load_all_quotes_from_db()
        messagebox.showinfo("Extraction Success", f"Successfully extracted and saved quotes from chat text under source: {source_name}!")
        window.destroy()

    def send_chat_message(self):
        msg = self.chat_entry.get().strip()
        if not msg:
            return
            
        self.chat_entry.delete(0, tk.END)
        
        # Enable textbox to insert user message
        self.chat_log.configure(state="normal")
        self.chat_log.insert(tk.END, f"\nYou: {msg}\n")
        self.chat_log.see(tk.END)
        self.chat_log.configure(state="disabled")
        
        # Save user message to database
        self.save_chat_message_to_db("You", msg)
        
        # Disable inputs
        self.btn_chat_send.configure(state="disabled")
        self.chat_entry.configure(state="disabled")
        
        # Start AI thread
        threading.Thread(target=self.run_chat_ai, args=(msg,), daemon=True).start()

    def run_chat_ai(self, user_msg):
        try:
            if not self.api_key:
                self.append_ai_response("Error: Please save your Gemini API key on the left panel first.")
                return
                
            # Format context from comparison table
            quotes_context = ""
            if self.extracted_data:
                quotes_context = json.dumps(self.extracted_data, indent=2)
            else:
                quotes_context = "The database is currently empty. No supplier quotes have been loaded."
                
            system_prompt = f"""
            You are an expert AI Procurement Assistant helping the user analyze their supplier quotations.
            Here is the current database of extracted supplier quotes:
            {quotes_context}
            
            Guidelines:
            - Provide direct, concise, and helpful answers.
            - Help compare prices, lead times, payment terms, and MOQs.
            - If asked to write a negotiation email, write a professional email tailored to the chosen supplier.
            - Keep answers friendly, highly professional, and focused on the data.
            """
            
            ai_response = self.generate_with_fallback(
                [],
                f"{system_prompt}\n\nUser Message: {user_msg}",
                json_response=False
            )
            
            self.append_ai_response(ai_response)
        except Exception as e:
            self.append_ai_response(f"Error generating response: {e}")

    def append_ai_response(self, response_text):
        self.after(0, lambda: self.finish_ai_response(response_text))

    def finish_ai_response(self, text):
        self.chat_log.configure(state="normal")
        self.chat_log.insert(tk.END, f"\nAI: {text}\n")
        self.chat_log.see(tk.END)
        self.chat_log.configure(state="disabled")
        
        # Save response message to SQLite
        self.save_chat_message_to_db("AI", text)
        
        self.btn_chat_send.configure(state="normal")
        self.chat_entry.configure(state="normal")
        self.chat_entry.focus()

    # --- Manual Edits logic ---
    def add_empty_row(self):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            INSERT INTO extracted_quotes (filename, supplier, product, spec, color, elastic, price, unit, moq, packing, term, lead_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "Manually Added", 
            "New Supplier", 
            "Product", 
            "Size / Gsm", 
            "Color", 
            "Single/Double", 
            0.0, 
            "piece", 
            "N/A", 
            "N/A", 
            "EXW/FOB", 
            "N/A"
        ))
        conn.commit()
        last_id = c.lastrowid
        conn.close()
        
        self.load_all_quotes_from_db()
        
        for item in self.tree.get_children():
            vals = self.tree.item(item, "values")
            if int(vals[0]) == last_id:
                self.tree.selection_set(item)
                self.edit_selected_row()
                break

    def delete_selected_row(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Select Row", "Please select a row to delete first.")
            return
            
        ans = messagebox.askyesno("Delete", "Are you sure you want to delete the selected row?")
        if ans:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            for item in sel:
                vals = self.tree.item(item, "values")
                q_id = int(vals[0])
                c.execute("DELETE FROM extracted_quotes WHERE id = ?", (q_id,))
            conn.commit()
            conn.close()
            self.load_all_quotes_from_db()

    def edit_selected_row(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Select Row", "Please select a row to edit first.")
            return
            
        item = sel[0]
        vals = self.tree.item(item, "values")
        q_id = int(vals[0])
        
        row_dict = None
        for r in self.extracted_data:
            if r["id"] == q_id:
                row_dict = r
                break
                
        if not row_dict:
            return
            
        edit_win = ctk.CTkToplevel(self)
        edit_win.title(f"Edit Quote Details (ID: {q_id})")
        edit_win.geometry("450x650")
        edit_win.resizable(False, False)
        edit_win.attributes("-topmost", True)
        
        labels = [
            ("Supplier Name:", "supplier"),
            ("Product Name:", "product"),
            ("Specifications:", "spec"),
            ("Color:", "color"),
            ("Elastic (Single/Double):", "elastic"),
            ("Unit Price:", "price"),
            ("Price Unit:", "unit"),
            ("MOQ:", "moq"),
            ("Packing Details:", "packing"),
            ("Price Term:", "term"),
            ("Delivery Lead Time:", "lead_time"),
            ("Validity Date:", "validity_date"),
            ("Risk Alerts:", "sourcing_risk")
        ]
        
        entries = {}
        for idx, (lbl_txt, field) in enumerate(labels):
            tk_lbl = ctk.CTkLabel(edit_win, text=lbl_txt, anchor="w")
            tk_lbl.grid(row=idx, column=0, padx=15, pady=5, sticky="ew")
            
            val = str(row_dict[field])
            tk_ent = ctk.CTkEntry(edit_win, width=250)
            tk_ent.insert(0, val)
            tk_ent.grid(row=idx, column=1, padx=15, pady=5, sticky="ew")
            entries[field] = tk_ent
            
        def save_changes():
            try:
                price_val = float(entries["price"].get().replace("$", "").replace("¥", "").replace("€", ""))
            except ValueError:
                messagebox.showerror("Invalid Input", "Price must be a numeric value.", parent=edit_win)
                return
                
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("""
                UPDATE extracted_quotes 
                SET supplier=?, product=?, spec=?, color=?, elastic=?, price=?, unit=?, moq=?, packing=?, term=?, lead_time=?, validity_date=?, sourcing_risk=?
                WHERE id=?
            """, (
                entries["supplier"].get(),
                entries["product"].get(),
                entries["spec"].get(),
                entries["color"].get(),
                entries["elastic"].get(),
                price_val,
                entries["unit"].get(),
                entries["moq"].get(),
                entries["packing"].get(),
                entries["term"].get(),
                entries["lead_time"].get(),
                entries["validity_date"].get(),
                entries["sourcing_risk"].get(),
                q_id
            ))
            conn.commit()
            conn.close()
            
            self.load_all_quotes_from_db()
            edit_win.destroy()
            
        btn_save = ctk.CTkButton(edit_win, text="Save Changes", fg_color="#1f7d44", hover_color="#15592e", command=save_changes)
        btn_save.grid(row=len(labels), column=0, columnspan=2, pady=15)

    # --- Export Logic (Beautiful Excel Report Designer) ---
    def export_to_excel(self):
        if not self.extracted_data:
            messagebox.showwarning("No Data", "There is no extracted data to export.")
            return
            
        file_path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel files", "*.xlsx")])
        if file_path:
            try:
                df = pd.DataFrame(self.extracted_data)
                df = df.drop(columns=["id"])
                
                # Retrieve active currency choices
                currency_choice = self.currency_cb.get()
                factor = 1.0
                price_col_header = "Unit Price (USD)"
                excel_format = '$#,##0.00000'
                
                if "CNY" in currency_choice:
                    factor = 7.25
                    price_col_header = "Unit Price (CNY)"
                    excel_format = '[$¥-804]#,##0.00000'
                elif "EUR" in currency_choice:
                    factor = 0.92
                    price_col_header = "Unit Price (EUR)"
                    excel_format = '[$€-2] #,##0.00000'

                # Apply active conversion to df export
                df["price"] = df["price"].apply(lambda p: float(p) * factor if isinstance(p, (int, float)) else p)
                df.columns = ["Source File", "Supplier", "Product", "Specifications", "Color", "Elastic", price_col_header, "Price Unit", "MOQ", "Packing Details", "Price Term", "Lead Time", "Validity Date", "Risk Alerts"]
                
                from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
                
                with pd.ExcelWriter(file_path, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name="Quotes Comparison")
                    workbook = writer.book
                    worksheet = writer.sheets["Quotes Comparison"]
                    
                    # Styles definitions
                    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid") # Navy Blue
                    header_font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
                    
                    alt_fill = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid") # Light Grey
                    best_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid") # Soft Green
                    
                    normal_font = Font(name="Segoe UI", size=10)
                    best_font = Font(name="Segoe UI", size=10, bold=True, color="375623") # Dark Green
                    
                    thin_side = Side(border_style="thin", color="D9D9D9")
                    thin_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
                    
                    align_center = Alignment(horizontal="center", vertical="center")
                    align_left = Alignment(horizontal="left", vertical="center")
                    
                    # Format Header Row
                    for col_num in range(1, len(df.columns) + 1):
                        cell = worksheet.cell(row=1, column=col_num)
                        cell.fill = header_fill
                        cell.font = header_font
                        cell.alignment = align_center
                        cell.border = thin_border
                    
                    # Format Data Rows & highlights
                    groups = {}
                    for r in self.extracted_data:
                        price = r.get("price")
                        try:
                            price = float(price)
                        except (ValueError, TypeError):
                            continue
                        if price <= 0:
                            continue
                        g_key = self.get_group_key(r)
                        if g_key not in groups:
                            groups[g_key] = []
                        groups[g_key].append(price)

                    best_prices = {}
                    for g_key, prices in groups.items():
                        if len(prices) > 1:
                            best_prices[g_key] = min(prices)

                    for row_idx, row_data in enumerate(self.extracted_data, start=2):
                        is_best = False
                        g_key = self.get_group_key(row_data)
                        try:
                            price_val = float(row_data["price"])
                            if g_key in best_prices and abs(price_val - best_prices[g_key]) < 1e-7:
                                is_best = True
                        except (ValueError, TypeError):
                            pass
                            
                        row_fill = best_fill if is_best else (alt_fill if row_idx % 2 == 0 else PatternFill(fill_type=None))
                        row_font = best_font if is_best else normal_font
                        
                        for col_idx in range(1, len(df.columns) + 1):
                            cell = worksheet.cell(row=row_idx, column=col_idx)
                            cell.fill = row_fill
                            cell.font = row_font
                            cell.border = thin_border
                            
                            # Alignments
                            if col_idx in [1, 2, 3, 4, 10]:  # Source, Supplier, Product, Specs, Packing
                                cell.alignment = align_left
                            else:
                                cell.alignment = align_center
                                
                            # Price custom formatting matching currency selected
                            if col_idx == 7:
                                cell.number_format = excel_format
                                
                    # Set custom row heights
                    worksheet.row_dimensions[1].height = 28
                    for r_idx in range(2, len(self.extracted_data) + 2):
                        worksheet.row_dimensions[r_idx].height = 20
                        
                    # Auto-fit columns
                    for col in worksheet.columns:
                        max_len = max(len(str(cell.value or '')) for cell in col)
                        col_letter = col[0].column_letter
                        worksheet.column_dimensions[col_letter].width = max(max_len + 4, 12)
                        
                messagebox.showinfo("Export Successful", f"Excel sheet exported successfully to:\n{file_path}")
            except Exception as e:
                messagebox.showerror("Export Failed", f"Could not write Excel file:\n{e}")

    def export_to_csv(self):
        if not self.extracted_data:
            messagebox.showwarning("No Data", "There is no extracted data to export.")
            return
            
        file_path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV files", "*.csv")])
        if file_path:
            try:
                df = pd.DataFrame(self.extracted_data)
                df = df.drop(columns=["id"])
                df.columns = ["Source File", "Supplier", "Product", "Specifications", "Color", "Elastic", "Unit Price (USD)", "Price Unit", "MOQ", "Packing Details", "Price Term", "Lead Time", "Validity Date", "Risk Alerts"]
                df.to_csv(file_path, index=False)
                messagebox.showinfo("Export Successful", f"CSV file exported successfully to:\n{file_path}")
            except Exception as e:
                messagebox.showerror("Export Failed", f"Could not write CSV file:\n{e}")

    def export_to_pdf(self):
        if not self.extracted_data:
            messagebox.showwarning("No Data", "There is no extracted data to generate a report.")
            return
            
        category_filter = self.insights_category_cb.get()
        
        # Filter quotes by selected category
        filtered_data = self.extracted_data
        if category_filter and category_filter != "All":
            filtered_data = [r for r in self.extracted_data if (r.get("product") or "").strip().lower() == category_filter.lower()]
            
        if not filtered_data:
            messagebox.showwarning("No Data", f"No quote data found for category '{category_filter}' to export.")
            return
            
        file_path = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF files", "*.pdf")])
        if not file_path:
            return
            
        # Run in a thread with a loading dialog since it asks Gemini for the executive summary
        loading_win = ctk.CTkToplevel(self)
        loading_win.title("Generating PDF Report")
        loading_win.geometry("350x120")
        loading_win.resizable(False, False)
        loading_win.attributes("-topmost", True)
        
        lbl = ctk.CTkLabel(loading_win, text="AI is writing executive summary...\nPlease wait...", font=ctk.CTkFont(size=12))
        lbl.pack(pady=30)
        
        def run_pdf_gen():
            try:
                # 1. Ask Gemini for executive summary
                quotes_summary_str = ""
                for idx, r in enumerate(filtered_data):
                    quotes_summary_str += f"- Supplier: {r.get('supplier')}, Product: {r.get('product')}, Spec: {r.get('spec')}, Price: {r.get('price')} USD, Lead Time: {r.get('lead_time')}, Validity: {r.get('validity_date')}, Sourcing Risk: {r.get('sourcing_risk')}\n"
                
                category_desc = f"category '{category_filter}'" if category_filter != "All" else "all categories"
                ai_prompt = f"""
                You are a senior global sourcing manager. Write a concise executive summary and recommendation report based on these supplier quotes for {category_desc}:
                {quotes_summary_str}
                
                Keep the summary strictly under 150 words. Focus on:
                - Who offers the best price.
                - Who is the fastest/safest option.
                - Sourcing warnings or risks to be aware of.
                Write in highly professional, executive language. Do not use markdown syntax.
                """
                
                # Call fallback generator
                try:
                    summary_text = self.generate_with_fallback([], ai_prompt, json_response=False)
                except Exception:
                    summary_text = f"Standard quote comparison report generated for {category_desc}. Please inspect details in the table below to determine supplier suitability based on pricing, lead time, and risk considerations."
                
                # 2. Build PDF Document using ReportLab
                from reportlab.lib.pagesizes import letter
                from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
                from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
                from reportlab.lib import colors
                
                doc = SimpleDocTemplate(file_path, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
                story = []
                
                styles = getSampleStyleSheet()
                
                # Custom Styles
                title_style = ParagraphStyle(
                    'DocTitle',
                    parent=styles['Heading1'],
                    fontSize=18,
                    leading=22,
                    textColor=colors.HexColor('#1f538d'),
                    spaceAfter=15
                )
                
                section_heading = ParagraphStyle(
                    'SectionHeading',
                    parent=styles['Heading2'],
                    fontSize=12,
                    leading=15,
                    textColor=colors.HexColor('#1f538d'),
                    spaceBefore=12,
                    spaceAfter=6,
                    keepWithNext=True
                )
                
                body_style = ParagraphStyle(
                    'BodyTextCustom',
                    parent=styles['BodyText'],
                    fontSize=9,
                    leading=12,
                    textColor=colors.HexColor('#2c3e50')
                )
                
                header_style = ParagraphStyle(
                    'TableHeader',
                    parent=body_style,
                    textColor=colors.white,
                    fontSize=8,
                    leading=10,
                    fontName='Helvetica-Bold'
                )
                
                cell_style = ParagraphStyle(
                    'TableCell',
                    parent=body_style,
                    fontSize=7.5,
                    leading=9
                )
                
                # Document Header
                story.append(Paragraph(f"SUPPLIER COMPARISON & SOURCING RISK REPORT ({category_filter.upper()})", title_style))
                story.append(Paragraph(f"<b>Date:</b> August 1, 2026 &nbsp;&nbsp;|&nbsp;&nbsp; <b>Total Quotes:</b> {len(filtered_data)}", body_style))
                story.append(Spacer(1, 10))
                
                # Divider Line
                d_table = Table([[""]], colWidths=[540], rowHeights=[2])
                d_table.setStyle(TableStyle([
                    ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#1f538d')),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 0),
                    ('TOPPADDING', (0,0), (-1,-1), 0),
                ]))
                story.append(d_table)
                story.append(Spacer(1, 10))
                
                # AI Executive Summary Section
                story.append(Paragraph("AI Executive Summary & Sourcing Advice", section_heading))
                story.append(Paragraph(summary_text.replace("\n", "<br/>"), body_style))
                story.append(Spacer(1, 15))
                
                # Comparison Table Section
                story.append(Paragraph("Supplier Quotation Grid Details", section_heading))
                
                # Build table headers & rows
                headers = [
                    Paragraph("Supplier", header_style),
                    Paragraph("Product", header_style),
                    Paragraph("Specs", header_style),
                    Paragraph("Price", header_style),
                    Paragraph("Lead Time", header_style),
                    Paragraph("Validity", header_style),
                    Paragraph("Risk Alerts", header_style)
                ]
                
                table_data = [headers]
                for r in filtered_data:
                    # Clean currency display for PDF table
                    currency_choice = self.currency_cb.get()
                    symbol = "$"
                    factor = 1.0
                    if "CNY" in currency_choice:
                        symbol = "¥"
                        factor = 7.25
                    elif "EUR" in currency_choice:
                        symbol = "€"
                        factor = 0.92
                        
                    try:
                        p_val = float(r["price"])
                        price_display = f"{symbol}{p_val * factor:.5f}"
                    except Exception:
                        price_display = str(r["price"])
                        
                    val_display = self.get_validity_display(r.get("validity_date"))
                    risk_display = self.get_risk_display(r.get("sourcing_risk"))
                    
                    row = [
                        Paragraph(self.clean_supplier_name(r.get("supplier")), cell_style),
                        Paragraph(r.get("product"), cell_style),
                        Paragraph(r.get("spec"), cell_style),
                        Paragraph(price_display, cell_style),
                        Paragraph(r.get("lead_time"), cell_style),
                        Paragraph(val_display, cell_style),
                        Paragraph(risk_display, cell_style)
                    ]
                    table_data.append(row)
                    
                # Setup column widths (Total width 540)
                col_widths = [80, 75, 90, 50, 50, 85, 110]
                t = Table(table_data, colWidths=col_widths, repeatRows=1)
                
                # Table style
                t_style = TableStyle([
                    ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1f538d')),
                    ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                    ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#dddddd')),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 4),
                    ('TOPPADDING', (0,0), (-1,-1), 4),
                ])
                
                # Alternating row colors
                for r_idx in range(1, len(table_data)):
                    bg = colors.HexColor('#ffffff') if r_idx % 2 == 1 else colors.HexColor('#f9f9f9')
                    t_style.add('BACKGROUND', (0, r_idx), (-1, r_idx), bg)
                    
                t.setStyle(t_style)
                story.append(t)
                story.append(Spacer(1, 15))
                
                # Footer note
                story.append(Spacer(1, 10))
                story.append(Paragraph("<i>Disclaimer: This report was generated automatically via the AI Supplier Quote Extractor based on extracted quotation records in your database. Please confirm terms directly with suppliers before committing.</i>", cell_style))
                
                doc.build(story)
                
                loading_win.after(0, loading_win.destroy)
                loading_win.after(0, lambda: messagebox.showinfo("Export Successful", f"PDF Sourcing Report generated successfully:\n{file_path}"))
                
            except Exception as e:
                loading_win.after(0, loading_win.destroy)
                import traceback
                traceback.print_exc()
                loading_win.after(0, lambda e_err=e: messagebox.showerror("Export Failed", f"Could not write PDF file:\n{e_err}"))
                
        threading.Thread(target=run_pdf_gen, daemon=True).start()

    def setup_sourcing_insights_tab(self):
        tab_insights = self.tabview.tab("💡 AI Sourcing Insights")
        tab_insights.grid_columnconfigure(0, weight=1)
        tab_insights.grid_columnconfigure(1, weight=1)
        tab_insights.grid_rowconfigure(1, weight=1)

        # Header Row Container
        header_row = ctk.CTkFrame(tab_insights, fg_color="transparent")
        header_row.grid(row=0, column=0, columnspan=2, padx=20, pady=(15, 5), sticky="ew")

        title_lbl = ctk.CTkLabel(header_row, text="AI Sourcing Insights & Negotiator", font=ctk.CTkFont(size=20, weight="bold"))
        title_lbl.pack(side="left")

        # Category Filter Combobox
        self.insights_category_cb = ctk.CTkComboBox(header_row, values=["All"], command=lambda choice: self.update_sourcing_insights(), width=150)
        self.insights_category_cb.pack(side="right", padx=10)
        self.insights_category_cb.set("All")

        insights_lbl = ctk.CTkLabel(header_row, text="Filter Product Category:")
        insights_lbl.pack(side="right", padx=5)

        # --- LEFT PANEL: AI Analysis Cards ---
        left_frame = ctk.CTkFrame(tab_insights)
        left_frame.grid(row=1, column=0, padx=10, pady=10, sticky="nsew")
        left_frame.grid_columnconfigure(0, weight=1)
        left_frame.grid_rowconfigure(1, weight=1)
        left_frame.grid_rowconfigure(2, weight=1)
        left_frame.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(left_frame, text="🏆 AI Sourcing Matrix Highlights", font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0, padx=15, pady=10, sticky="w")

        # Cards for Cheapest, Fastest, Safest
        self.cheapest_card = ctk.CTkTextbox(left_frame, height=90, activate_scrollbars=False, wrap="word")
        self.cheapest_card.grid(row=1, column=0, padx=15, pady=5, sticky="ew")
        
        self.fastest_card = ctk.CTkTextbox(left_frame, height=90, activate_scrollbars=False, wrap="word")
        self.fastest_card.grid(row=2, column=0, padx=15, pady=5, sticky="ew")
        
        self.safest_card = ctk.CTkTextbox(left_frame, height=90, activate_scrollbars=False, wrap="word")
        self.safest_card.grid(row=3, column=0, padx=15, pady=5, sticky="ew")

        # Build PDF Button inside left_frame under cards
        self.btn_export_pdf = ctk.CTkButton(left_frame, text="📄 Build PDF Report", fg_color="#8c1c1c", hover_color="#6e1313", command=self.export_to_pdf)
        self.btn_export_pdf.grid(row=4, column=0, padx=15, pady=(10, 15), sticky="ew")

        # --- RIGHT PANEL: Counter-Offer Email Writer ---
        right_frame = ctk.CTkFrame(tab_insights)
        right_frame.grid(row=1, column=1, padx=10, pady=10, sticky="nsew")
        right_frame.grid_columnconfigure(0, weight=1)
        right_frame.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(right_frame, text="✉ One-Click Negotiation Email Generator", font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0, padx=15, pady=10, sticky="w")

        ctrl_row = ctk.CTkFrame(right_frame, fg_color="transparent")
        ctrl_row.grid(row=1, column=0, padx=15, pady=5, sticky="ew")

        # Select Supplier
        ctk.CTkLabel(ctrl_row, text="Supplier:").pack(side="left", padx=2)
        self.negotiate_supplier_cb = ctk.CTkComboBox(ctrl_row, values=["No suppliers loaded"], width=120)
        self.negotiate_supplier_cb.pack(side="left", padx=2)

        # Select Goal
        ctk.CTkLabel(ctrl_row, text="Goal:").pack(side="left", padx=2)
        self.negotiate_goal_cb = ctk.CTkComboBox(ctrl_row, values=["Price Match", "Better Terms", "Fast Delivery", "Lower MOQ"], width=110)
        self.negotiate_goal_cb.pack(side="left", padx=2)

        # Select Channel
        ctk.CTkLabel(ctrl_row, text="Channel:").pack(side="left", padx=2)
        self.negotiate_channel_btn = ctk.CTkSegmentedButton(ctrl_row, values=["Email", "Chat"], width=110)
        self.negotiate_channel_btn.pack(side="left", padx=2)
        self.negotiate_channel_btn.set("Email")

        self.btn_gen_email = ctk.CTkButton(ctrl_row, text="✍ Generate", width=70, fg_color="#1f538d", command=self.generate_negotiation_email)
        self.btn_gen_email.pack(side="left", padx=5)

        # Textbox for output email
        self.email_text_box = ctk.CTkTextbox(right_frame, wrap="word")
        self.email_text_box.grid(row=2, column=0, padx=15, pady=10, sticky="nsew")

        # Copy and Launch buttons row
        action_row = ctk.CTkFrame(right_frame, fg_color="transparent")
        action_row.grid(row=3, column=0, padx=15, pady=(5, 15), sticky="ew")

        self.btn_copy_email = ctk.CTkButton(action_row, text="📋 Copy Text", width=130, fg_color="#1f7d44", hover_color="#15592e", command=self.copy_email_to_clipboard)
        self.btn_copy_email.pack(side="left", padx=(0, 5))

        self.btn_launch_email = ctk.CTkButton(action_row, text="📧 Launch Email", width=140, fg_color="#1f538d", command=self.launch_email_client)
        self.btn_launch_email.pack(side="left", padx=(5, 0))

    def update_sourcing_insights(self):
        if not self.extracted_data:
            self.set_card_text(self.cheapest_card, "💵 Cheapest Option\nNo quotes data loaded in database.")
            self.set_card_text(self.fastest_card, "⚡ Fastest Option\nNo quotes data loaded in database.")
            self.set_card_text(self.safest_card, "🛡 Safest Option (Lowest Risk)\nNo quotes data loaded in database.")
            self.negotiate_supplier_cb.configure(values=["No suppliers loaded"])
            self.negotiate_supplier_cb.set("No suppliers loaded")
            return

        category_filter = self.insights_category_cb.get()
        
        # Filter quotes by selected category
        filtered_data = self.extracted_data
        if category_filter and category_filter != "All":
            filtered_data = [r for r in self.extracted_data if (r.get("product") or "").strip().lower() == category_filter.lower()]

        if not filtered_data:
            self.set_card_text(self.cheapest_card, f"💵 Cheapest Option\nNo quotes found for category '{category_filter}'.")
            self.set_card_text(self.fastest_card, f"⚡ Fastest Option\nNo quotes found for category '{category_filter}'.")
            self.set_card_text(self.safest_card, f"🛡 Safest Option (Lowest Risk)\nNo quotes found for category '{category_filter}'.")
            self.negotiate_supplier_cb.configure(values=["No suppliers loaded"])
            self.negotiate_supplier_cb.set("No suppliers loaded")
            return

        # 1. Update dropdown values to only show suppliers who offer the selected product category
        suppliers = sorted(list({r.get("supplier") for r in filtered_data if r.get("supplier") and r.get("supplier") != "Unknown"}))
        if suppliers:
            self.negotiate_supplier_cb.configure(values=suppliers)
            self.negotiate_supplier_cb.set(suppliers[0])
        else:
            self.negotiate_supplier_cb.configure(values=["No suppliers loaded"])
            self.negotiate_supplier_cb.set("No suppliers loaded")

        # 2. Extract Cheapest Option
        cheapest_row = None
        min_price = float('inf')
        for r in filtered_data:
            try:
                p = float(r["price"])
                if p < min_price:
                    min_price = p
                    cheapest_row = r
            except Exception:
                pass
        
        if cheapest_row:
            self.set_card_text(self.cheapest_card, f"💵 CHEAPEST OPTION:\nSupplier: {self.clean_supplier_name(cheapest_row['supplier'])}\nProduct: {cheapest_row['product']} - Price: ${cheapest_row['price']:.5f} / pc\nSpecs: {cheapest_row['spec']}")
        else:
            self.set_card_text(self.cheapest_card, "💵 Cheapest Option\nNo numeric price quotes found.")

        # 3. Extract Fastest Option (Lowest lead time)
        fastest_row = None
        min_days = float('inf')
        for r in filtered_data:
            lt_str = r.get("lead_time") or ""
            import re
            match = re.search(r'\d+', lt_str)
            if match:
                days = int(match.group(0))
                if days < min_days:
                    min_days = days
                    fastest_row = r
        
        if fastest_row:
            self.set_card_text(self.fastest_card, f"⚡ FASTEST OPTION:\nSupplier: {self.clean_supplier_name(fastest_row['supplier'])}\nProduct: {fastest_row['product']} - Lead Time: {fastest_row['lead_time']}\nPrice: ${fastest_row['price']} / pc")
        else:
            self.set_card_text(self.fastest_card, "⚡ Fastest Option\nNo specific lead times found.")

        # 4. Extract Safest Option (Lowest risk, active validity date)
        safest_row = None
        for r in filtered_data:
            risk = (r.get("sourcing_risk") or "").lower()
            val = (r.get("validity_date") or "").lower()
            if "low" in risk and "expired" not in val:
                safest_row = r
                break
        
        if not safest_row:
            for r in filtered_data:
                risk = (r.get("sourcing_risk") or "").lower()
                if "low" in risk:
                    safest_row = r
                    break
        
        if not safest_row and filtered_data:
            safest_row = filtered_data[0]

        if safest_row:
            self.set_card_text(self.safest_card, f"🛡 SAFEST OPTION (LOWEST RISK):\nSupplier: {self.clean_supplier_name(safest_row['supplier'])}\nProduct: {safest_row.get('product')}\nRisk Level: {safest_row.get('sourcing_risk')}\nValidity: {safest_row.get('validity_date')}")
        else:
            self.set_card_text(self.safest_card, "🛡 Safest Option (Lowest Risk)\nNo risk evaluation details found.")

    def set_card_text(self, card_widget, text):
        card_widget.configure(state="normal")
        card_widget.delete("1.0", tk.END)
        card_widget.insert("1.0", text)
        card_widget.configure(state="disabled")

    def generate_negotiation_email(self):
        supplier = self.negotiate_supplier_cb.get()
        goal = self.negotiate_goal_cb.get()
        if not supplier or supplier == "No suppliers loaded":
            messagebox.showwarning("Select Supplier", "Please select a valid supplier to negotiate with.")
            return

        self.btn_gen_email.configure(state="disabled", text="Writing...")
        
        def run_email_gen():
            try:
                category_filter = self.insights_category_cb.get()
                supplier_quotes = [q for q in self.extracted_data if q.get("supplier") == supplier]
                other_quotes = [q for q in self.extracted_data if q.get("supplier") != supplier]
                
                # Apply product category filtering
                if category_filter and category_filter != "All":
                    supplier_quotes = [q for q in supplier_quotes if (q.get("product") or "").strip().lower() == category_filter.lower()]
                    other_quotes = [q for q in other_quotes if (q.get("product") or "").strip().lower() == category_filter.lower()]
                
                context_str = f"Supplier Quotation Details:\n{json.dumps(supplier_quotes, indent=2)}\n\nOther Competitor Quotes:\n{json.dumps(other_quotes, indent=2)}"
                
                ai_prompt = f"""
                You are an expert global procurement advisor. Write a professional, polite, and persuasive negotiation email to the supplier '{supplier}'.
                Goal of negotiation: {goal}.
                
                Here is the current quotation database context for references:
                {context_str}
                
                Make sure:
                - Use the actual competitor pricing or lead times from the context to negotiate a better deal (e.g., if a competitor offers Cap at $0.0059 and this supplier quoted $0.0071, ask them to match $0.0059 or meet half-way).
                - Sound highly professional, polite, yet firm.
                - Keep placeholders for sender name/company.
                - Output ONLY the ready-to-copy email body. No markdown formatting.
                """
                
                email_body = self.generate_with_fallback([], ai_prompt, json_response=False)
                
                self.after(0, lambda text=email_body: self.display_negotiation_email(text))
            except Exception as e:
                self.after(0, lambda err=e: self.display_negotiation_email(f"Error generating negotiation email: {err}"))
                
        threading.Thread(target=run_email_gen, daemon=True).start()

    def display_negotiation_email(self, text):
        self.btn_gen_email.configure(state="normal", text="✍ Generate")
        self.email_text_box.delete("1.0", tk.END)
        self.email_text_box.insert("1.0", text)

    def copy_email_to_clipboard(self):
        email_content = self.email_text_box.get("1.0", tk.END).strip()
        if not email_content:
            return
        self.clipboard_clear()
        self.clipboard_append(email_content)
        messagebox.showinfo("Copied", "Negotiation email copied to clipboard successfully!")

    def select_files(self):
        file_paths = filedialog.askopenfilenames(
            title="Select Supplier Quotes",
            filetypes=[("Quote files", "*.pdf;*.png;*.jpg;*.jpeg;*.txt;*.xlsx;*.xls")]
        )
        if not file_paths:
            return
            
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT filename FROM processed_files")
        processed = {row[0] for row in c.fetchall()}
        conn.close()
        
        # If no folder was selected, default folder path to the parent directory of the first file
        if not self.selected_folder and file_paths:
            self.selected_folder = os.path.dirname(file_paths[0])
            self.folder_entry.delete(0, tk.END)
            self.folder_entry.insert(0, self.selected_folder)
            
        existing_names = {item[0] for item in self.files_list}
        
        for path in file_paths:
            name = os.path.basename(path)
            if name not in existing_names:
                self.files_list.append((name, path))
                if name in processed:
                    self.files_box_synced.insert(tk.END, f"✅ {name}")
                else:
                    self.files_box_unsynced.insert(tk.END, f"⏳ {name}")
                    
        has_unsynced = any(name not in processed for (name, path) in self.files_list)
        if has_unsynced:
            self.btn_start.configure(state="normal")

    def clean_filename_part(self, s):
        if not s:
            return ""
        import re
        # Remove anything that isn't alphanumeric or space
        s = re.sub(r'[^a-zA-Z0-9\s-]', '', s)
        # Convert spaces to CamelCase
        return "".join(word.capitalize() for word in s.split())

    def start_file_organizer_thread(self):
        if not self.selected_folder:
            messagebox.showerror("Folder Missing", "Please select a quotes folder or load files first.")
            return
            
        ans = messagebox.askyesno(
            "Organize Files",
            "This will create a new folder 'Organized_Quotes' inside your selected folder.\n\nIt will rename and copy your quotation files into subfolders sorted by Supplier Name (e.g. 'Organized_Quotes/XiantaoTopmed/XiantaoTopmed_Hairnet_20260801.pdf').\n\nWould you like to proceed?"
        )
        if not ans:
            return
            
        threading.Thread(target=self.run_file_organizer, daemon=True).start()

    def run_file_organizer(self):
        try:
            # Query all quotes
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT filename, supplier, product, validity_date FROM extracted_quotes")
            rows = c.fetchall()
            conn.close()
            
            if not rows:
                self.after(0, lambda: messagebox.showwarning("No Data", "No quotes found in database to organize."))
                return
                
            # Group by unique source filename to avoid duplicating the same source file
            from collections import defaultdict
            grouped = defaultdict(list)
            for filename, supplier, product, validity in rows:
                grouped[filename].append((supplier, product, validity))
                
            organized_dir = os.path.join(self.selected_folder, "Organized_Quotes")
            os.makedirs(organized_dir, exist_ok=True)
            
            copied_count = 0
            filename_to_path = {item[0]: item[1] for item in self.files_list}
            
            for filename, items in grouped.items():
                src_path = filename_to_path.get(filename)
                if not src_path:
                    src_path = os.path.join(self.selected_folder, filename)
                    
                if not os.path.exists(src_path):
                    continue
                    
                # Use details from the first item to name the file
                supplier, first_product, validity = items[0]
                
                # Get unique products in this file
                unique_products = list(dict.fromkeys([it[1] for it in items if it[1] and it[1] != "N/A"]))
                
                if len(unique_products) > 1:
                    clean_product = self.clean_filename_part(unique_products[0]) or "Product"
                    clean_product = f"{clean_product}_etc"
                elif len(unique_products) == 1:
                    clean_product = self.clean_filename_part(unique_products[0]) or "Product"
                else:
                    clean_product = "Product"
                    
                clean_supplier = self.clean_filename_part(supplier) or "UnknownSupplier"
                
                # Format date
                date_part = ""
                if validity and validity != "N/A":
                    date_part = "_" + validity.replace("-", "")
                
                ext = os.path.splitext(filename)[1]
                new_filename = f"{clean_supplier}_{clean_product}{date_part}{ext}"
                
                supplier_dir = os.path.join(organized_dir, clean_supplier)
                os.makedirs(supplier_dir, exist_ok=True)
                
                dest_path = os.path.join(supplier_dir, new_filename)
                
                import shutil
                shutil.copy2(src_path, dest_path)
                copied_count += 1
                
            self.after(0, lambda: messagebox.showinfo("Success", f"Quotes organized successfully!\nCopied and renamed {copied_count} files into:\n{organized_dir}"))
        except Exception as e:
            self.after(0, lambda err=e: messagebox.showerror("Error", f"Failed to organize files: {err}"))

    def setup_scorecard_tab(self):
        tab_scorecard = self.tabview.tab("🏆 Supplier Scorecard")
        tab_scorecard.grid_columnconfigure(0, weight=3)
        tab_scorecard.grid_columnconfigure(1, weight=1)
        tab_scorecard.grid_rowconfigure(1, weight=1)
        tab_scorecard.grid_rowconfigure(2, weight=0)

        # Header Frame (Spans both columns)
        header_row = ctk.CTkFrame(tab_scorecard, fg_color="transparent")
        header_row.grid(row=0, column=0, columnspan=2, padx=20, pady=(15, 5), sticky="ew")

        title_lbl = ctk.CTkLabel(header_row, text="Supplier Scorecard Matrix", font=ctk.CTkFont(size=20, weight="bold"))
        title_lbl.pack(side="left")

        # Category Filter Combobox
        self.scorecard_category_cb = ctk.CTkComboBox(header_row, values=["All"], command=lambda choice: self.update_scorecard_tab(), width=150)
        self.scorecard_category_cb.pack(side="right", padx=10)
        self.scorecard_category_cb.set("All")

        scorecard_lbl = ctk.CTkLabel(header_row, text="Filter Product Category:")
        scorecard_lbl.pack(side="right", padx=5)

        # --- LEFT COLUMN: Treeview Scorecard Table ---
        table_frame = ctk.CTkFrame(tab_scorecard, fg_color="#2b2b2b")
        table_frame.grid(row=1, column=0, padx=(20, 10), pady=10, sticky="nsew")
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)

        # Set up custom styles for Scorecard treeview
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Scorecard.Treeview",
                        background="#2b2b2b",
                        foreground="white",
                        rowheight=30,
                        fieldbackground="#2b2b2b",
                        borderwidth=0,
                        font=("Segoe UI", 10))
        style.map("Scorecard.Treeview", background=[('selected', '#1f538d')])
        style.configure("Scorecard.Treeview.Heading",
                        background="#1f538d",
                        foreground="white",
                        font=("Segoe UI", 10, "bold"),
                        borderwidth=0)

        cols = ("Rank", "Supplier", "Product", "Price", "MOQ", "Lead Time", "Risk", "Score", "Rating")
        self.scorecard_tree = ttk.Treeview(table_frame, columns=cols, show="headings", style="Scorecard.Treeview")
        
        col_widths = {
            "Rank": 45,
            "Supplier": 110,
            "Product": 95,
            "Price": 70,
            "MOQ": 70,
            "Lead Time": 80,
            "Risk": 90,
            "Score": 60,
            "Rating": 80
        }
        
        for col in cols:
            self.scorecard_tree.heading(col, text=col)
            self.scorecard_tree.column(col, width=col_widths.get(col, 90), anchor="center" if col in ["Rank", "Price", "MOQ", "Score", "Rating"] else "w")

        # Scrollbar
        sb = ttk.Scrollbar(table_frame, orient="vertical", command=self.scorecard_tree.yview)
        self.scorecard_tree.configure(yscrollcommand=sb.set)
        
        self.scorecard_tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        # Recommendation Text Card at bottom
        self.rec_card = ctk.CTkTextbox(tab_scorecard, height=75, wrap="word", font=ctk.CTkFont(size=12))
        self.rec_card.grid(row=2, column=0, padx=(20, 10), pady=(5, 15), sticky="ew")

        # --- RIGHT COLUMN: Sourcing Weight Simulator ---
        sim_frame = ctk.CTkFrame(tab_scorecard)
        sim_frame.grid(row=1, column=1, rowspan=2, padx=(10, 20), pady=10, sticky="nsew")
        sim_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(sim_frame, text="🎛 Sourcing Priorities", font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0, padx=15, pady=(15, 10), sticky="w")

        # Initial default weights
        self.weight_price = 40
        self.weight_lead = 20
        self.weight_moq = 20
        self.weight_risk = 20

        # Sliders
        self.lbl_w_price = ctk.CTkLabel(sim_frame, text="Price Weight: 40%", font=ctk.CTkFont(size=11))
        self.lbl_w_price.grid(row=1, column=0, padx=15, pady=(10, 2), sticky="w")
        self.slider_price = ctk.CTkSlider(sim_frame, from_=0, to=100, number_of_steps=100, command=lambda v: self.on_weight_changed("price", v))
        self.slider_price.grid(row=2, column=0, padx=15, pady=2, sticky="ew")
        self.slider_price.set(40)

        self.lbl_w_lead = ctk.CTkLabel(sim_frame, text="Lead Time Weight: 20%", font=ctk.CTkFont(size=11))
        self.lbl_w_lead.grid(row=3, column=0, padx=15, pady=(10, 2), sticky="w")
        self.slider_lead = ctk.CTkSlider(sim_frame, from_=0, to=100, number_of_steps=100, command=lambda v: self.on_weight_changed("lead", v))
        self.slider_lead.grid(row=4, column=0, padx=15, pady=2, sticky="ew")
        self.slider_lead.set(20)

        self.lbl_w_moq = ctk.CTkLabel(sim_frame, text="MOQ Weight: 20%", font=ctk.CTkFont(size=11))
        self.lbl_w_moq.grid(row=5, column=0, padx=15, pady=(10, 2), sticky="w")
        self.slider_moq = ctk.CTkSlider(sim_frame, from_=0, to=100, number_of_steps=100, command=lambda v: self.on_weight_changed("moq", v))
        self.slider_moq.grid(row=6, column=0, padx=15, pady=2, sticky="ew")
        self.slider_moq.set(20)

        self.lbl_w_risk = ctk.CTkLabel(sim_frame, text="Risk Weight: 20%", font=ctk.CTkFont(size=11))
        self.lbl_w_risk.grid(row=7, column=0, padx=15, pady=(10, 2), sticky="w")
        self.slider_risk = ctk.CTkSlider(sim_frame, from_=0, to=100, number_of_steps=100, command=lambda v: self.on_weight_changed("risk", v))
        self.slider_risk.grid(row=8, column=0, padx=15, pady=2, sticky="ew")
        self.slider_risk.set(20)

        # Reset button
        btn_reset_weights = ctk.CTkButton(sim_frame, text="Reset Defaults", command=self.reset_weights, fg_color="#3c3c3c")
        btn_reset_weights.grid(row=9, column=0, padx=15, pady=25, sticky="ew")

    def update_scorecard_tab(self):
        # Clear existing rows
        self.scorecard_tree.delete(*self.scorecard_tree.get_children())
        
        # Calculate scorecard
        matrix = self.calculate_supplier_scorecard()
        if not matrix:
            self.set_card_text(self.rec_card, "🏆 AI Recommendation Scorecard\nNo quotes data available in database to rank.")
            return

        for idx, row in enumerate(matrix):
            price_display = f"${float(row['price']):.5f}" if row['price'] is not None else "N/A"
            self.scorecard_tree.insert("", "end", values=(
                f"#{idx+1}",
                self.clean_supplier_name(row["supplier"]),
                row["product"],
                price_display,
                row["moq"] or "N/A",
                row["lead_time"] or "N/A",
                row["risk"] or "N/A",
                f"{row['score']}/100",
                row["stars"]
            ))

        # Highlight best option in recommendation text
        best = matrix[0]
        best_supplier = self.clean_supplier_name(best["supplier"])
        self.set_card_text(self.rec_card, f"🏆 TOP SOURCING RECOMMENDATION:\nBased on our AI Scorecard algorithm, {best_supplier} is the highly recommended choice for {best['product']} with an overall score of {best['score']}/100. They offer a highly competitive price of ${float(best['price']):.5f}/pc, MOQ of {best['moq'] or 'N/A'}, and lead time of {best['lead_time'] or 'N/A'} with a risk level of: {best['risk'] or 'N/A'}.")

    def calculate_supplier_scorecard(self):
        if not hasattr(self, 'scorecard_category_cb'):
            return []
            
        category = self.scorecard_category_cb.get()
        
        # Filter quotes by selected category
        filtered_data = self.extracted_data
        if category and category != "All":
            filtered_data = [r for r in self.extracted_data if (r.get("product") or "").strip().lower() == category.lower()]
            
        if not filtered_data:
            return []
            
        # Parse numeric prices, lead times, and MOQs for scaling
        prices = []
        lead_times = []
        moqs = []
        
        for r in filtered_data:
            try:
                if r["price"] is not None:
                    prices.append(float(r["price"]))
            except (ValueError, TypeError):
                pass
            import re
            lt_match = re.search(r'\d+', r.get("lead_time") or "")
            if lt_match:
                lead_times.append(int(lt_match.group(0)))
            moq_match = re.search(r'\d+', (r.get("moq") or "").replace(",", ""))
            if moq_match:
                moqs.append(int(moq_match.group(0)))
                
        min_price = min(prices) if prices else 0.001
        max_price = max(prices) if prices else 1.0
        
        min_lead = min(lead_times) if lead_times else 5
        max_lead = max(lead_times) if lead_times else 30
        
        min_moq = min(moqs) if moqs else 5000
        max_moq = max(moqs) if moqs else 100000
        
        scorecard = []
        for r in filtered_data:
            supplier = r.get("supplier") or "Unknown"
            product = r.get("product") or "Unknown"
            
            # --- Price Score (100 pts max) ---
            try:
                p_val = float(r["price"])
                if max_price == min_price:
                    price_score = 100.0
                else:
                    price_score = 100.0 * (1.0 - (p_val - min_price) / (max_price - min_price))
            except Exception:
                price_score = 40.0
                
            # --- Lead Time Score (100 pts max) ---
            import re
            lt_match = re.search(r'\d+', r.get("lead_time") or "")
            if lt_match:
                lt_val = int(lt_match.group(0))
                if max_lead == min_lead:
                    lt_score = 100.0
                else:
                    lt_score = 100.0 * (1.0 - (lt_val - min_lead) / (max_lead - min_lead))
            else:
                lt_score = 50.0
                
            # --- MOQ Score (100 pts max) ---
            moq_match = re.search(r'\d+', (r.get("moq") or "").replace(",", ""))
            if moq_match:
                moq_val = int(moq_match.group(0))
                if max_moq == min_moq:
                    moq_score = 100.0
                else:
                    moq_score = 100.0 * (1.0 - (moq_val - min_moq) / (max_moq - min_moq))
            else:
                moq_score = 50.0
                
            # --- Risk Score (100 pts max) ---
            risk = (r.get("sourcing_risk") or "").lower()
            if "high" in risk:
                risk_score = 20.0
            elif "medium" in risk:
                risk_score = 60.0
            elif "low" in risk:
                risk_score = 100.0
            else:
                risk_score = 50.0
                
            # Weighted average
            total_w = self.weight_price + self.weight_lead + self.weight_moq + self.weight_risk
            if total_w == 0:
                total_w = 1.0
            total_score = (price_score * self.weight_price + lt_score * self.weight_lead + moq_score * self.weight_moq + risk_score * self.weight_risk) / total_w
            
            star_rating = "⭐" * int(round(total_score / 20.0))
            if not star_rating:
                star_rating = "⭐"
                
            scorecard.append({
                "supplier": supplier,
                "product": product,
                "price": r.get("price"),
                "moq": r.get("moq"),
                "lead_time": r.get("lead_time"),
                "risk": r.get("sourcing_risk"),
                "score": int(round(total_score)),
                "stars": star_rating
            })
            
        scorecard.sort(key=lambda x: x["score"], reverse=True)
        return scorecard

    def on_weight_changed(self, weight_type, value):
        val = int(value)
        if weight_type == "price":
            self.weight_price = val
            self.lbl_w_price.configure(text=f"Price Weight: {val}%")
        elif weight_type == "lead":
            self.weight_lead = val
            self.lbl_w_lead.configure(text=f"Lead Time Weight: {val}%")
        elif weight_type == "moq":
            self.weight_moq = val
            self.lbl_w_moq.configure(text=f"MOQ Weight: {val}%")
        elif weight_type == "risk":
            self.weight_risk = val
            self.lbl_w_risk.configure(text=f"Risk Weight: {val}%")
            
        self.update_scorecard_tab()

    def reset_weights(self):
        self.weight_price = 40
        self.weight_lead = 20
        self.weight_moq = 20
        self.weight_risk = 20
        
        self.slider_price.set(40)
        self.slider_lead.set(20)
        self.slider_moq.set(20)
        self.slider_risk.set(20)
        
        self.lbl_w_price.configure(text="Price Weight: 40%")
        self.lbl_w_lead.configure(text="Lead Time Weight: 20%")
        self.lbl_w_moq.configure(text="MOQ Weight: 20%")
        self.lbl_w_risk.configure(text="Risk Weight: 20%")
        
        self.update_scorecard_tab()

    def launch_email_client(self):
        supplier = self.negotiate_supplier_cb.get()
        body = self.email_text_box.get("1.0", tk.END).strip()
        if not body:
            return
            
        email = ""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT contact_info FROM supplier_contacts WHERE supplier = ?", (supplier,))
        row = c.fetchone()
        conn.close()
        
        if row and row[0]:
            contact_str = row[0]
            import re
            emails = re.findall(r'[\w\.-]+@[\w\.-]+', contact_str)
            if emails:
                email = emails[0]
                
        subject = f"Price Negotiation - Quotation Review"
        import urllib.parse
        import webbrowser
        mailto_url = f"mailto:{email}?subject={urllib.parse.quote(subject)}&body={urllib.parse.quote(body)}"
        try:
            webbrowser.open(mailto_url)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open email client:\n{e}")

    def update_preview_metrics_overlay(self, vals):
        for widget in self.preview_metrics_frame.winfo_children():
            widget.destroy()

        if not vals:
            ctk.CTkLabel(self.preview_metrics_frame, text="Select a quote to view key details", font=("Segoe UI", 10, "italic"), text_color="grey").pack(pady=10)
            return

        self.preview_metrics_frame.grid_columnconfigure(0, weight=1)
        self.preview_metrics_frame.grid_columnconfigure(1, weight=2)

        # Title Label
        ctk.CTkLabel(self.preview_metrics_frame, text="🔍 Extracted AI Sourcing Metrics", font=ctk.CTkFont(size=11, weight="bold"), text_color="#a6e3e9").grid(row=0, column=0, columnspan=2, pady=(8, 4))

        metrics = [
            ("Supplier:", vals[2], "#1f538d"),
            ("Product:", vals[3], "#15592e"),
            ("Price:", f"${vals[7]} / {vals[8]}", "#6e4513"),
            ("MOQ:", vals[9], "#5a1f1f"),
            ("Lead Time:", vals[12], "#1f538d"),
            ("Validity:", vals[13], "#6e4513"),
            ("Risk Alert:", vals[14], "#5a1f1f")
        ]

        for idx, (label, val, color) in enumerate(metrics):
            lbl = ctk.CTkLabel(self.preview_metrics_frame, text=label, font=ctk.CTkFont(size=10, weight="bold"), anchor="w")
            lbl.grid(row=idx+1, column=0, sticky="w", padx=10, pady=2)
            
            # Format status values nicely
            val_str = str(val)
            val_lbl = ctk.CTkLabel(self.preview_metrics_frame, text=val_str, font=ctk.CTkFont(size=10), anchor="w", wraplength=180, justify="left")
            val_lbl.grid(row=idx+1, column=1, sticky="w", padx=10, pady=2)

    def setup_timeline_tab(self):
        tab_timeline = self.tabview.tab("📅 Sourcing Timeline")
        tab_timeline.grid_columnconfigure(0, weight=1)
        tab_timeline.grid_columnconfigure(1, weight=1)
        tab_timeline.grid_rowconfigure(1, weight=1)

        # Header Title
        title_lbl = ctk.CTkLabel(tab_timeline, text="Sourcing Timeline & Expiration Planner", font=ctk.CTkFont(size=20, weight="bold"))
        title_lbl.grid(row=0, column=0, columnspan=2, padx=20, pady=(15, 10), sticky="w")

        # --- LEFT PANEL: Expiration Alerts ---
        left_frame = ctk.CTkFrame(tab_timeline)
        left_frame.grid(row=1, column=0, padx=10, pady=10, sticky="nsew")
        left_frame.grid_columnconfigure(0, weight=1)
        left_frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(left_frame, text="📅 Quote Expiration Alerts", font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0, padx=15, pady=10, sticky="w")
        
        self.expiration_scroll = ctk.CTkScrollableFrame(left_frame, fg_color="#2b2b2b")
        self.expiration_scroll.grid(row=1, column=0, padx=15, pady=10, sticky="nsew")
        self.expiration_scroll.grid_columnconfigure(0, weight=1)

        # --- RIGHT PANEL: Production Planner ---
        right_frame = ctk.CTkFrame(tab_timeline)
        right_frame.grid(row=1, column=1, padx=10, pady=10, sticky="nsew")
        right_frame.grid_columnconfigure(0, weight=1)
        right_frame.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(right_frame, text="⚡ Lead Time Production Planner", font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0, padx=15, pady=10, sticky="w")

        # Filters
        filter_row = ctk.CTkFrame(right_frame, fg_color="transparent")
        filter_row.grid(row=1, column=0, padx=15, pady=5, sticky="ew")

        ctk.CTkLabel(filter_row, text="Category:").pack(side="left", padx=5)
        self.timeline_category_cb = ctk.CTkComboBox(filter_row, values=["All"], command=lambda c: self.update_timeline_tab(), width=130)
        self.timeline_category_cb.pack(side="left", padx=5)
        self.timeline_category_cb.set("All")

        ctk.CTkLabel(filter_row, text="Order Date:").pack(side="left", padx=5)
        self.order_date_entry = ctk.CTkEntry(filter_row, width=110)
        self.order_date_entry.pack(side="left", padx=5)
        self.order_date_entry.insert(0, "2026-08-03")
        self.order_date_entry.bind("<Return>", lambda e: self.update_timeline_tab())
        self.order_date_entry.bind("<FocusOut>", lambda e: self.update_timeline_tab())

        # Timeline display scroll frame
        self.timeline_display_scroll = ctk.CTkScrollableFrame(right_frame, fg_color="#2b2b2b")
        self.timeline_display_scroll.grid(row=2, column=0, padx=15, pady=10, sticky="nsew")
        self.timeline_display_scroll.grid_columnconfigure(0, weight=1)

    def update_timeline_tab(self):
        # 1. Update category values
        if hasattr(self, 'timeline_category_cb'):
            products = set()
            for r in self.extracted_data:
                prod = (r.get("product") or "").strip().title()
                if prod:
                    products.add(prod)
            sorted_prods = ["All"] + sorted(list(products))
            current = self.timeline_category_cb.get()
            self.timeline_category_cb.configure(values=sorted_prods)
            if current in sorted_prods:
                self.timeline_category_cb.set(current)
            else:
                self.timeline_category_cb.set("All")

        # 2. Populate Expiration list
        for widget in self.expiration_scroll.winfo_children():
            widget.destroy()

        import datetime
        today = datetime.date(2026, 8, 3)
        
        if not self.extracted_data:
            ctk.CTkLabel(self.expiration_scroll, text="No quotes data loaded in database.", text_color="grey").pack(pady=20)
        else:
            for idx, r in enumerate(self.extracted_data):
                supplier = self.clean_supplier_name(r.get("supplier"))
                product = r.get("product") or "Product"
                val_date_str = r.get("validity_date")
                
                status_text = "🟢 Active"
                text_color = "#a6ffa6"
                bg_color = "#1e4620"
                
                if val_date_str and val_date_str != "N/A":
                    try:
                        val_date = datetime.datetime.strptime(val_date_str, "%Y-%m-%d").date()
                        delta = (val_date - today).days
                        if delta < 0:
                            status_text = f"🔴 Expired (on {val_date_str})"
                            text_color = "#ffa6a6"
                            bg_color = "#4d1e1e"
                        elif delta <= 14:
                            status_text = f"🟡 Expiry Warning ({delta} days left)"
                            text_color = "#ffefa6"
                            bg_color = "#4d3d1e"
                        else:
                            status_text = f"🟢 Active ({delta} days left)"
                    except Exception:
                        pass
                else:
                    status_text = "⚪ Validity Unknown"
                    text_color = "#cccccc"
                    bg_color = "#3a3a3a"

                row_fr = ctk.CTkFrame(self.expiration_scroll, fg_color=bg_color, height=45)
                row_fr.pack(fill="x", pady=3, padx=5)
                
                ctk.CTkLabel(row_fr, text=f"{supplier} - {product}", font=ctk.CTkFont(weight="bold")).pack(side="left", padx=10)
                ctk.CTkLabel(row_fr, text=status_text, text_color=text_color, font=ctk.CTkFont(weight="bold")).pack(side="right", padx=10)

        # 3. Populate Production Planner progress bars
        for widget in self.timeline_display_scroll.winfo_children():
            widget.destroy()

        category = self.timeline_category_cb.get()
        filtered = self.extracted_data
        if category and category != "All":
            filtered = [r for r in self.extracted_data if (r.get("product") or "").strip().lower() == category.lower()]

        # Parse Order Date
        order_date_str = self.order_date_entry.get().strip()
        try:
            order_date = datetime.datetime.strptime(order_date_str, "%Y-%m-%d").date()
        except Exception:
            order_date = today

        if not filtered:
            ctk.CTkLabel(self.timeline_display_scroll, text="No quotes matching selected category.", text_color="grey").pack(pady=20)
            return

        max_days = 30
        rows_to_plot = []
        for r in filtered:
            lt_str = r.get("lead_time") or ""
            import re
            match = re.search(r'\d+', lt_str)
            days = 0
            if match:
                days = int(match.group(0))
            if days > max_days:
                max_days = days
            rows_to_plot.append((r, days))

        for r, days in rows_to_plot:
            supplier = self.clean_supplier_name(r.get("supplier"))
            product = r.get("product") or "Product"
            
            completion_date = order_date + datetime.timedelta(days=days)
            comp_date_str = completion_date.strftime("%Y-%m-%d")

            bar_fr = ctk.CTkFrame(self.timeline_display_scroll, fg_color="transparent")
            bar_fr.pack(fill="x", pady=6, padx=5)

            lbl_fr = ctk.CTkFrame(bar_fr, fg_color="transparent")
            lbl_fr.pack(fill="x")
            
            ctk.CTkLabel(lbl_fr, text=f"{supplier} ({product})", font=ctk.CTkFont(weight="bold")).pack(side="left")
            ctk.CTkLabel(lbl_fr, text=f"Est Delivery: {comp_date_str} ({days} days)", text_color="#a6e3e9").pack(side="right")

            # Progress bar
            progress = ctk.CTkProgressBar(bar_fr, height=12)
            progress.pack(fill="x", pady=(2, 8))
            progress.set(days / max_days)
            if days <= 15:
                progress.configure(progress_color="#368b85")
            elif days <= 30:
                progress.configure(progress_color="#1f538d")
            else:
                progress.configure(progress_color="#d65a31")

    def update_preview_gallery_bar(self, row_id, vals):
        for widget in self.preview_gallery_bar.winfo_children():
            widget.destroy()

        if not vals:
            return

        attached_media_str = ""
        for r in self.extracted_data:
            if str(r.get("id")) == str(row_id):
                attached_media_str = r.get("attached_media") or ""
                break

        options = [("📄 Quote", "quote", vals[1])]
        
        if attached_media_str:
            media_files = attached_media_str.split(";")
            img_idx = 1
            vid_idx = 1
            for m in media_files:
                if m.strip():
                    path = m.strip()
                    ext = os.path.splitext(path)[1].lower()
                    if ext in [".mp4", ".avi", ".mov", ".mkv", ".mpg", ".mpeg", ".wmv"]:
                        options.append((f"🎥 Video {vid_idx}", "video", path))
                        vid_idx += 1
                    elif ext in [".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"]:
                        options.append((f"🖼 Image {img_idx}", "image", path))
                        img_idx += 1
                    else:
                        options.append((f"📎 File {img_idx}", "other", path))
                        img_idx += 1

        self.gallery_buttons = {}
        for idx, (label, media_type, path) in enumerate(options):
            btn = ctk.CTkButton(
                self.preview_gallery_bar,
                text=label,
                width=80,
                height=26,
                fg_color="#1f538d" if idx == 0 else "#3c3c3c",
                command=lambda p=path, t=media_type, idx=idx: self.select_gallery_item(p, t, idx)
            )
            btn.pack(side="left", padx=3, pady=2)
            self.gallery_buttons[idx] = btn

    def select_gallery_item(self, path, media_type, btn_idx):
        for idx, btn in self.gallery_buttons.items():
            if idx == btn_idx:
                btn.configure(fg_color="#1f538d")
            else:
                btn.configure(fg_color="#3c3c3c")

        self.preview_text_box.pack_forget()
        self.preview_image_lbl.pack_forget()
        for widget in self.preview_display_frame.winfo_children():
            if widget not in [self.preview_image_lbl, self.preview_text_box]:
                widget.destroy()

        if media_type == "quote":
            self.show_preview(path)
            return

        if not path or not os.path.exists(path):
            self.show_preview_message(f"Attached file not found:\n{os.path.basename(path)}")
            self.btn_open_external.configure(state="disabled")
            return

        self.current_preview_path = path
        self.btn_open_external.configure(state="normal")

        if media_type == "video":
            self.show_video_play_overlay(path)
        elif media_type == "image":
            self.render_image_preview(path)
        else:
            self.show_preview_message(f"Attached file:\n{os.path.basename(path)}\n(Click button below to open.)")

    def show_video_play_overlay(self, path):
        for widget in self.preview_display_frame.winfo_children():
            if widget not in [self.preview_image_lbl, self.preview_text_box]:
                widget.destroy()
                
        self.preview_image_lbl.pack_forget()
        self.preview_text_box.pack_forget()
        
        play_btn = ctk.CTkButton(self.preview_display_frame, text="▶ Play Video", font=ctk.CTkFont(size=16, weight="bold"), fg_color="#8c1c1c", hover_color="#6e1313", height=50, command=lambda: os.startfile(path))
        play_btn.pack(expand=True, pady=100, padx=20)

    def attach_media_to_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Warning", "Please select a quotation row to attach media to.")
            return

        item = sel[0]
        vals = self.tree.item(item, "values")
        row_id = vals[0]
        supplier = vals[2]
        product = vals[3]

        files = filedialog.askopenfilenames(
            title="Attach Images/Videos to Quotation",
            filetypes=[("Media Files", "*.png;*.jpg;*.jpeg;*.gif;*.webp;*.bmp;*.mp4;*.avi;*.mov;*.mkv;*.wmv")]
        )
        if not files:
            return

        # Setup top-level options window
        options_win = ctk.CTkToplevel(self)
        options_win.title("Attachment Scope")
        options_win.geometry("450x260")
        options_win.resizable(False, False)
        options_win.attributes("-topmost", True)
        
        # Center the window
        options_win.update_idletasks()
        width = options_win.winfo_width()
        height = options_win.winfo_height()
        x = (options_win.winfo_screenwidth() // 2) - (width // 2)
        y = (options_win.winfo_screenheight() // 2) - (height // 2)
        options_win.geometry(f'+{x}+{y}')

        ctk.CTkLabel(options_win, text="Select Attachment Scope:", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(15, 10), padx=20, anchor="w")
        
        scope_var = tk.IntVar(value=1)
        
        r1 = ctk.CTkRadioButton(options_win, text="Selected quote row only", variable=scope_var, value=1)
        r1.pack(pady=5, padx=30, anchor="w")

        r2 = ctk.CTkRadioButton(options_win, text=f"All quotes from {supplier} for '{product}'", variable=scope_var, value=2)
        r2.pack(pady=5, padx=30, anchor="w")

        r3 = ctk.CTkRadioButton(options_win, text=f"All quotes from {supplier} (All products)", variable=scope_var, value=3)
        r3.pack(pady=5, padx=30, anchor="w")

        btn_row = ctk.CTkFrame(options_win, fg_color="transparent")
        btn_row.pack(fill="x", side="bottom", pady=15, padx=20)

        def proceed():
            choice = scope_var.get()
            options_win.destroy()
            self.execute_media_attachment(row_id, supplier, product, files, choice)

        def cancel():
            options_win.destroy()

        btn_cancel = ctk.CTkButton(btn_row, text="Cancel", width=100, fg_color="#3c3c3c", command=cancel)
        btn_cancel.pack(side="right", padx=5)

        btn_confirm = ctk.CTkButton(btn_row, text="Attach Media", width=120, fg_color="#1f538d", command=proceed)
        btn_confirm.pack(side="right", padx=5)

    def execute_media_attachment(self, row_id, supplier, product, files, choice):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        # Determine target query based on choice
        if choice == 1:
            c.execute("SELECT id, attached_media FROM extracted_quotes WHERE id = ?", (row_id,))
        elif choice == 2:
            c.execute("SELECT id, attached_media FROM extracted_quotes WHERE supplier = ? AND product = ?", (supplier, product))
        else:
            c.execute("SELECT id, attached_media FROM extracted_quotes WHERE supplier = ?", (supplier,))
            
        rows = c.fetchall()
        
        updated_count = 0
        for r_id, existing_media_str in rows:
            media_list = []
            if existing_media_str:
                media_list = [f.strip() for f in existing_media_str.split(";") if f.strip()]

            for f in files:
                norm_f = os.path.abspath(f).replace("\\", "/")
                if norm_f not in media_list:
                    media_list.append(norm_f)

            new_val = ";".join(media_list)
            c.execute("UPDATE extracted_quotes SET attached_media = ? WHERE id = ?", (new_val, r_id))
            updated_count += 1

        conn.commit()
        conn.close()

        self.load_all_quotes_from_db()
        
        scope_lbls = {
            1: "selected quote only",
            2: f"all {supplier} quotes for '{product}'",
            3: f"all quotes from {supplier}"
        }
        messagebox.showinfo("Success", f"Attached {len(files)} media files to {updated_count} quotation(s) ({scope_lbls[choice]}) successfully!")

    def move_file_to_synced(self, name):
        for idx in range(self.files_box_unsynced.size()):
            item_text = self.files_box_unsynced.get(idx)
            if name in item_text:
                self.files_box_unsynced.delete(idx)
                break
        self.files_box_synced.insert(tk.END, f"✅ {name}")

if __name__ == "__main__":
    app = App()
    app.mainloop()

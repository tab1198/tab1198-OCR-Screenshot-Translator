import tkinter as tk
from tkinter import Toplevel, messagebox
from PIL import ImageGrab, Image, ImageTk, ImageDraw
import pytesseract
import requests
import random
import hashlib
import json
import os
import threading
import sys
import ctypes
import pystray
from pynput import keyboard

# ================= 适配高分屏 =================
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

# ================= 配置区域 (智能 Tesseract 路径) =================
# 【重要】这段代码会自动检测：是运行在开发环境，还是运行在打包后的环境
if getattr(sys, 'frozen', False):
    # 如果是打包后的 .exe，则获取 exe 所在的目录
    application_path = os.path.dirname(sys.executable)
else:
    # 如果是运行 .py 脚本，则获取脚本所在的目录
    application_path = os.path.dirname(os.path.abspath(__file__))

# 1. 优先检查：当前程序同级目录下是否有 Tesseract-OCR 文件夹 (这是给安装包用的)
portable_tesseract = os.path.join(application_path, 'Tesseract-OCR', 'tesseract.exe')

if os.path.exists(portable_tesseract):
    # 如果找到了内置的，就强制使用内置的
    pytesseract.pytesseract.tesseract_cmd = portable_tesseract
else:
    # 2. 如果没找到，才去系统默认路径找 (开发调试用)
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
CONFIG_FILE = 'baidu_config.json'
BOX_COLOR = 'red'
BOX_WIDTH = 4
TEXT_BG_COLOR = '#333333'
TEXT_FG_COLOR = 'white'

class BaiduTranslator:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        
        self.config = self.load_config()
        self.app_id = self.config.get('app_id', '')
        self.secret_key = self.config.get('secret_key', '')
        self.current_char = self.config.get('shortcut_char', 'z')
        
        self.current_hotkey = f'<ctrl>+<alt>+{self.current_char}'
        
        self.is_running_task = False 
        self.selection_window = None
        self.result_window = None
        self.start_x = None
        self.start_y = None
        
        self.listener = None
        self.start_hotkey_listener()
        
        threading.Thread(target=self.setup_tray_icon, daemon=True).start()

    # ------------------ 🚀 百度批量翻译核心 (提速关键) ------------------
    def baidu_batch_translate(self, text_list):
        """
        将多行文本合并为一个请求发送，极大减少网络延迟
        """
        if not self.app_id or not self.secret_key:
            raise Exception("请先在设置中配置API")
        
        if not text_list: return []

        # 1. 用换行符拼接所有句子
        query = '\n'.join(text_list)
        
        endpoint = 'http://api.fanyi.baidu.com/api/trans/vip/translate'
        salt = random.randint(32768, 65536)
        sign = hashlib.md5((self.app_id + query + str(salt) + self.secret_key).encode()).hexdigest()
        
        try:
            params = {
                'q': query,
                'from': 'auto',
                'to': 'zh',
                'appid': self.app_id,
                'salt': salt,
                'sign': sign
            }
            res = requests.post(endpoint, params=params).json()
            
            if 'error_code' in res:
                raise Exception(f"API错误码: {res['error_code']}\n(52003=未授权, 54003=频率过快)")
                
            if 'trans_result' in res:
                # 返回结果列表，通常顺序与输入一致
                return [item['dst'] for item in res['trans_result']]
            
            return []
            
        except Exception as e:
            raise Exception(f"网络请求失败: {str(e)[:20]}...")

    # ------------------ 热键逻辑 ------------------
    def start_hotkey_listener(self):
        if self.listener:
            try: self.listener.stop()
            except: pass
        try:
            self.current_hotkey = f'<ctrl>+<alt>+{self.current_char}'
            self.listener = keyboard.GlobalHotKeys({
                self.current_hotkey: self.on_hotkey_activate
            })
            self.listener.start()
            print(f"热键已就绪: {self.current_hotkey}")
        except Exception as e:
            print(f"热键错误: {e}")

    def on_hotkey_activate(self):
        if self.is_running_task: return
        self.root.after(0, self.start_selection)

    # ------------------ 截图选区 ------------------
    def start_selection(self):
        self.is_running_task = True 
        if self.result_window: 
            self.result_window.destroy()
            self.result_window = None

        self.selection_window = Toplevel(self.root)
        self.selection_window.attributes("-fullscreen", True)
        self.selection_window.attributes("-topmost", True)
        self.selection_window.attributes("-alpha", 0.4) # 百度版稍微暗一点
        self.selection_window.configure(bg="black")
        
        self.canvas = tk.Canvas(self.selection_window, cursor="cross", bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.selection_window.bind("<Escape>", lambda e: self.reset_state())

    def reset_state(self):
        if self.selection_window: self.selection_window.destroy()
        if self.result_window: self.result_window.destroy()
        self.is_running_task = False

    def on_press(self, event):
        self.start_x = event.x
        self.start_y = event.y
        self.rect = self.canvas.create_rectangle(self.start_x, self.start_y, 1, 1, outline=BOX_COLOR, width=BOX_WIDTH)

    def on_drag(self, event):
        self.canvas.coords(self.rect, self.start_x, self.start_y, event.x, event.y)

    def on_release(self, event):
        if not self.start_x: return
        x1, y1 = min(self.start_x, event.x), min(self.start_y, event.y)
        x2, y2 = max(self.start_x, event.x), max(self.start_y, event.y)
        
        if (x2 - x1) < 10 or (y2 - y1) < 10:
            self.reset_state()
            return
            
        if self.selection_window:
            self.selection_window.destroy()
            self.selection_window = None
        
        self.root.update()
        
        try:
            full_img = ImageGrab.grab()
        except Exception:
            self.reset_state()
            return

        self.show_processing_ui(full_img, x1, y1, x2, y2)
        threading.Thread(target=self.thread_task, args=(full_img, x1, y1, x2, y2)).start()

    def show_processing_ui(self, img, x1, y1, x2, y2):
        self.result_window = Toplevel(self.root)
        self.result_window.attributes("-fullscreen", True)
        self.result_window.attributes("-topmost", True)
        
        self.bg_photo = ImageTk.PhotoImage(img)
        self.res_canvas = tk.Canvas(self.result_window, highlightthickness=0)
        self.res_canvas.pack(fill="both", expand=True)
        self.res_canvas.create_image(0, 0, image=self.bg_photo, anchor="nw")
        
        self.res_canvas.create_rectangle(x1, y1, x2, y2, outline=BOX_COLOR, width=BOX_WIDTH)
        self.loading_id = self.res_canvas.create_text(x1, y1-25, text="百度翻译中...", fill=BOX_COLOR, font=("微软雅黑", 12, "bold"), anchor="sw")
        self.res_canvas.bind("<Button-1>", lambda e: self.reset_state())

    # ------------------ 后台处理 (批量优化) ------------------
    def thread_task(self, full_img, x1, y1, x2, y2):
        try:
            crop = full_img.crop((x1, y1, x2, y2))
            
            # 使用 'eng' 模式以确保速度
            # 如果你有中文包且需要识别中文原图，请改为 'eng+chi_sim'
            data = pytesseract.image_to_data(crop, lang='eng', output_type=pytesseract.Output.DICT)
            
            # 1. 整理 OCR 数据
            lines_map = self.organize_ocr_data(data)
            
            # 2. 准备批量翻译列表
            source_texts = []
            ordered_keys = []
            
            for k, v in lines_map.items():
                line_str = " ".join(v['txt'])
                source_texts.append(line_str)
                ordered_keys.append(k)
            
            if not source_texts:
                 self.root.after(0, lambda: self.update_ui_finish([], x1, y1))
                 return

            # 3. 🚀 一次性发送请求
            translated_texts = self.baidu_batch_translate(source_texts)
            
            # 4. 匹配结果
            results = []
            # 防止 API 返回的数量不一致 (极少情况)
            count = min(len(translated_texts), len(ordered_keys))
            
            for i in range(count):
                key = ordered_keys[i]
                orig = lines_map[key]
                trans = translated_texts[i]
                results.append({
                    'text': trans, 
                    'x': orig['x'], 
                    'y': orig['y'], 
                    'w': orig['w'], 
                    'h': orig['h']
                })
            
            self.root.after(0, lambda: self.update_ui_finish(results, x1, y1))
            
        except Exception as e:
            self.root.after(0, lambda: self.show_error_popup(str(e)))

    def show_error_popup(self, msg):
        self.reset_state()
        messagebox.showerror("运行出错", f"详情: {msg}")

    def organize_ocr_data(self, data):
        lines = {}
        for i in range(len(data['level'])):
            text = data['text'][i].strip()
            if not text: continue
            k = (data['block_num'][i], data['line_num'][i])
            if k not in lines:
                lines[k] = {'txt': [], 'x': data['left'][i], 'y': data['top'][i], 'w': data['width'][i], 'h': data['height'][i]}
            else:
                lines[k]['txt'].append(text)
                lines[k]['w'] = (data['left'][i] + data['width'][i]) - lines[k]['x']
                lines[k]['h'] = max(lines[k]['h'], data['height'][i])
        return lines

    def update_ui_finish(self, results, off_x, off_y):
        if not self.result_window: return
        self.res_canvas.delete(self.loading_id)
        
        if not results:
            self.res_canvas.create_text(off_x, off_y-25, text="未识别到文字", fill="red", font=("微软雅黑", 10, "bold"), anchor="sw")
            return

        for item in results:
            fs = max(10, int(item['h'] * 0.7))
            lbl = tk.Label(self.res_canvas, text=item['text'], fg=TEXT_FG_COLOR, bg=TEXT_BG_COLOR,
                           font=("微软雅黑", fs, "bold"), wraplength=item['w']+250, justify="left")
            lbl.bind("<Button-1>", lambda e: self.reset_state())
            self.res_canvas.create_window(off_x + item['x'], off_y + item['y'], window=lbl, anchor="nw")

    # ------------------ 设置界面 ------------------
    def create_tray_image(self):
        image = Image.new('RGB', (64, 64), "#1E90FF")
        dc = ImageDraw.Draw(image)
        dc.rectangle((20, 20, 44, 44), fill="white")
        return image

    def setup_tray_icon(self):
        menu = (pystray.MenuItem('设置', self.open_settings), pystray.MenuItem('退出', self.quit_app))
        self.icon = pystray.Icon("baidu_trans", self.create_tray_image(), "百度截图翻译", menu)
        self.icon.run()

    def open_settings(self, icon, item):
        self.root.after(0, self._show_settings_window)

    def _show_settings_window(self):
        sw = Toplevel(self.root)
        sw.title("设置 - 百度翻译")
        sw.geometry("380x350")
        x = (sw.winfo_screenwidth() - 380) // 2
        y = (sw.winfo_screenheight() - 350) // 2
        sw.geometry(f"+{x}+{y}")
        sw.attributes("-topmost", True)
        sw.resizable(False, False)

        tk.Label(sw, text="百度 API 设置", font=("微软雅黑", 11, "bold"), fg="#1E90FF").pack(pady=15)
        
        f = tk.Frame(sw)
        f.pack(pady=5, padx=10)
        tk.Label(f, text="APP ID:", width=8).grid(row=0, column=0, pady=5)
        id_var = tk.StringVar(value=self.app_id)
        tk.Entry(f, textvariable=id_var, width=32).grid(row=0, column=1, pady=5)
        
        tk.Label(f, text="密钥:", width=8).grid(row=1, column=0, pady=5)
        key_var = tk.StringVar(value=self.secret_key)
        tk.Entry(f, textvariable=key_var, width=32, show="*").grid(row=1, column=1, pady=5)

        tk.Frame(sw, height=2, bg="#eee", width=300).pack(pady=15)
        
        tk.Label(sw, text="快捷键 (Ctrl + Alt + ?)", font=("微软雅黑", 11, "bold")).pack()
        hf = tk.Frame(sw)
        hf.pack(pady=10)
        tk.Label(hf, text="Ctrl + Alt + ", font=("Arial", 12)).pack(side=tk.LEFT)
        char_var = tk.StringVar(value=self.current_char)
        def v(P): return len(P) <= 1
        tk.Entry(hf, textvariable=char_var, width=4, font=("Arial", 12, "bold"), 
                 justify='center', bg="#f0f0f0", validate="key", validatecommand=(sw.register(v), '%P')).pack(side=tk.LEFT)

        def save():
            self.app_id = id_var.get().strip()
            self.secret_key = key_var.get().strip()
            c = char_var.get().strip().lower()
            self.current_char = c if c else 'z'
            self.save_config()
            self.start_hotkey_listener()
            messagebox.showinfo("成功", "设置已保存")
            sw.destroy()

        tk.Button(sw, text="保存设置", command=save, bg="#1E90FF", fg="white", font=("微软雅黑", 10, "bold")).pack(pady=20, ipady=5, fill="x", padx=60)

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f: return json.load(f)
            except: pass
        return {"app_id": "", "secret_key": "", "shortcut_char": "z"}

    def save_config(self):
        data = {"app_id": self.app_id, "secret_key": self.secret_key, "shortcut_char": self.current_char}
        try:
            with open(CONFIG_FILE, 'w') as f: json.dump(data, f, indent=4)
        except: pass

    def quit_app(self, icon, item):
        self.icon.stop()
        self.root.quit()
        sys.exit()

if __name__ == '__main__':
    app = BaiduTranslator()
    app.root.mainloop()
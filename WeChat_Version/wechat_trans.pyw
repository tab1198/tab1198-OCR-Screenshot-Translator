import tkinter as tk
from tkinter import Toplevel, messagebox
from PIL import ImageGrab, Image, ImageTk, ImageDraw
import pytesseract
import json
import os
import threading
import sys
import ctypes
import pystray
from pynput import keyboard

# ================= 依赖检查 =================
try:
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
    from tencentcloud.tmt.v20180321 import tmt_client, models
except ImportError:
    ctypes.windll.user32.MessageBoxW(0, "缺少依赖库，请运行 pip install tencentcloud-sdk-python", "错误", 0x10)
    sys.exit()

# ================= 适配高分屏 =================
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

# ================= 配置常量 (智能路径检测) =================
import sys
if getattr(sys, 'frozen', False):
    application_path = os.path.dirname(sys.executable)
else:
    application_path = os.path.dirname(os.path.abspath(__file__))

# 优先检查内置 Tesseract
portable_tesseract = os.path.join(application_path, 'Tesseract-OCR', 'tesseract.exe')
if os.path.exists(portable_tesseract):
    pytesseract.pytesseract.tesseract_cmd = portable_tesseract
else:
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

CONFIG_FILE = 'wechat_config.json'
BOX_COLOR = '#1AAD19'     # 微信绿
BOX_WIDTH = 2             
TEXT_BG_COLOR = '#2e2e2e' 
TEXT_FG_COLOR = 'white'   

class WeChatTranslator:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        
        self.config = self.load_config()
        self.secret_id = self.config.get('secret_id', '')
        self.secret_key = self.config.get('secret_key', '')
        self.current_char = self.config.get('shortcut_char', 'z')
        
        self.current_hotkey = f'<ctrl>+<alt>+{self.current_char}'
        
        # 窗口变量
        self.selection_window = None
        self.result_window = None
        self.start_x = None
        self.start_y = None
        
        # 监听器变量
        self.listener = None
        self.start_hotkey_listener()
        
        threading.Thread(target=self.setup_tray_icon, daemon=True).start()

    # ------------------ 🚀 极速批量翻译核心 ------------------
    def tencent_batch_translate(self, text_list):
        if not self.secret_id or not self.secret_key:
            raise Exception("请先在设置中配置密钥")
        
        if not text_list: return []

        try:
            cred = credential.Credential(self.secret_id, self.secret_key)
            httpProfile = HttpProfile()
            httpProfile.endpoint = "tmt.tencentcloudapi.com"
            httpProfile.reqTimeout = 10 
            
            clientProfile = ClientProfile()
            clientProfile.httpProfile = httpProfile
            client = tmt_client.TmtClient(cred, "ap-beijing", clientProfile)
            
            # 使用 Batch 批量请求
            req = models.TextTranslateBatchRequest()
            req.Source = "auto"
            req.Target = "zh"
            req.ProjectId = 0
            req.SourceTextList = text_list 
            
            resp = client.TextTranslateBatch(req)
            return resp.TargetTextList 
            
        except TencentCloudSDKException as err:
            raise Exception(f"API错误: {err.code}")
        except Exception as e:
            raise Exception(f"网络错误: {str(e)[:20]}...")

    # ------------------ 热键逻辑 (防死机核心) ------------------
    def start_hotkey_listener(self):
        """启动监听器"""
        self.stop_hotkey_listener() # 先清理旧的
        try:
            self.current_hotkey = f'<ctrl>+<alt>+{self.current_char}'
            self.listener = keyboard.GlobalHotKeys({
                self.current_hotkey: self.on_hotkey_activate
            })
            self.listener.start()
            print(f"监听已启动: {self.current_hotkey}")
        except Exception as e:
            print(f"热键错误: {e}")

    def stop_hotkey_listener(self):
        """停止监听器 (释放鼠标控制权)"""
        if self.listener:
            try:
                self.listener.stop()
            except: pass
            self.listener = None

    def on_hotkey_activate(self):
        # 【关键】收到热键后，立刻停止监听！防止和截图选区冲突
        self.stop_hotkey_listener()
        # 进入主线程开始截图
        self.root.after(0, self.start_selection)

    # ------------------ 截图选区 ------------------
    def start_selection(self):
        # 清理旧窗口
        if self.result_window: 
            self.result_window.destroy()
            self.result_window = None

        self.selection_window = Toplevel(self.root)
        self.selection_window.attributes("-fullscreen", True)
        self.selection_window.attributes("-topmost", True)
        self.selection_window.attributes("-alpha", 0.3)
        self.selection_window.configure(bg="black")
        
        # 强制获取焦点，确保鼠标事件能被捕获
        self.selection_window.focus_force()
        
        self.canvas = tk.Canvas(self.selection_window, cursor="cross", bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        # 按 ESC 取消截图并恢复监听
        self.selection_window.bind("<Escape>", lambda e: self.cancel_selection())

    def cancel_selection(self):
        if self.selection_window:
            self.selection_window.destroy()
            self.selection_window = None
        # 【关键】任务取消，必须重启监听，否则快捷键就失效了
        self.start_hotkey_listener()

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
        
        # 选区太小，视为误触
        if (x2 - x1) < 10 or (y2 - y1) < 10:
            self.cancel_selection()
            return
            
        # 关闭选区窗口
        if self.selection_window:
            self.selection_window.destroy()
            self.selection_window = None
        
        self.root.update()
        
        try:
            full_img = ImageGrab.grab()
        except Exception:
            self.cancel_selection()
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
        self.loading_id = self.res_canvas.create_text(x1, y1-25, text="微信翻译中...", fill=BOX_COLOR, font=("微软雅黑", 10, "bold"), anchor="sw")
        self.res_canvas.bind("<Button-1>", lambda e: self.close_result_and_restart())

    def close_result_and_restart(self):
        """关闭结果窗口，并重启监听器"""
        if self.result_window:
            self.result_window.destroy()
            self.result_window = None
        # 【关键】一切结束后，重启监听器，准备下一次截图
        self.start_hotkey_listener()

    # ------------------ 后台处理 ------------------
    def thread_task(self, full_img, x1, y1, x2, y2):
        try:
            crop = full_img.crop((x1, y1, x2, y2))
            
            # 使用 'eng' 确保速度
            data = pytesseract.image_to_data(crop, lang='eng', output_type=pytesseract.Output.DICT)
            
            lines_map = self.organize_ocr_data(data) 
            
            source_texts = []
            ordered_keys = [] 
            
            for k, v in lines_map.items():
                line_str = " ".join(v['txt'])
                source_texts.append(line_str)
                ordered_keys.append(k)
            
            if not source_texts:
                 # 没文字，也要结束流程并重启监听
                 self.root.after(0, lambda: self.update_ui_finish([], x1, y1))
                 return

            # 批量发送
            translated_texts = self.tencent_batch_translate(source_texts)
            
            results = []
            for i, trans_text in enumerate(translated_texts):
                key = ordered_keys[i]
                orig_data = lines_map[key]
                results.append({
                    'text': trans_text, 
                    'x': orig_data['x'], 
                    'y': orig_data['y'], 
                    'w': orig_data['w'], 
                    'h': orig_data['h']
                })
            
            self.root.after(0, lambda: self.update_ui_finish(results, x1, y1))
            
        except Exception as e:
            self.root.after(0, lambda: self.show_error_popup(str(e)))

    def show_error_popup(self, msg):
        # 出错也要重启监听
        if self.result_window: self.result_window.destroy()
        self.start_hotkey_listener()
        messagebox.showerror("运行出错", f"错误详情: {msg}")

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
                           font=("微软雅黑", fs), wraplength=item['w']+250, justify="left", padx=5, pady=2)
            
            # 点击任何一个文字标签，也都调用“关闭并重启监听”
            lbl.bind("<Button-1>", lambda e: self.close_result_and_restart())
            
            self.res_canvas.create_window(off_x + item['x'], off_y + item['y'], window=lbl, anchor="nw")

    # ------------------ 设置与托盘 ------------------
    def create_tray_image(self):
        image = Image.new('RGB', (64, 64), "#1AAD19")
        dc = ImageDraw.Draw(image)
        dc.rectangle((20, 20, 44, 44), fill="white")
        return image

    def setup_tray_icon(self):
        menu = (pystray.MenuItem('设置', self.open_settings), pystray.MenuItem('退出', self.quit_app))
        self.icon = pystray.Icon("wechat_trans", self.create_tray_image(), "微信截图翻译", menu)
        self.icon.run()

    def open_settings(self, icon, item):
        self.root.after(0, self._show_settings_window)

    def _show_settings_window(self):
        # 打开设置时，也要先暂停监听，防止按键冲突
        self.stop_hotkey_listener()
        
        sw = Toplevel(self.root)
        sw.title("设置")
        sw.geometry("380x350")
        x = (sw.winfo_screenwidth() - 380) // 2
        y = (sw.winfo_screenheight() - 350) // 2
        sw.geometry(f"+{x}+{y}")
        sw.attributes("-topmost", True)
        sw.resizable(False, False)

        tk.Label(sw, text="腾讯云 API 设置", font=("微软雅黑", 11, "bold"), fg="#1AAD19").pack(pady=15)
        
        f = tk.Frame(sw)
        f.pack(pady=5, padx=10)
        tk.Label(f, text="SecretId:", width=8).grid(row=0, column=0, pady=5)
        id_var = tk.StringVar(value=self.secret_id)
        tk.Entry(f, textvariable=id_var, width=32).grid(row=0, column=1, pady=5)
        
        tk.Label(f, text="SecretKey:", width=8).grid(row=1, column=0, pady=5)
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
            self.secret_id = id_var.get().strip()
            self.secret_key = key_var.get().strip()
            c = char_var.get().strip().lower()
            self.current_char = c if c else 'z'
            self.save_config()
            self.start_hotkey_listener()
            messagebox.showinfo("成功", "设置已保存")
            sw.destroy()

        def on_close():
            sw.destroy()
            self.start_hotkey_listener() # 关闭设置窗口，恢复监听

        tk.Button(sw, text="保存设置", command=save, bg="#1AAD19", fg="white", font=("微软雅黑", 10, "bold")).pack(pady=20, ipady=5, fill="x", padx=60)
        sw.protocol("WM_DELETE_WINDOW", on_close)

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f: return json.load(f)
            except: pass
        return {"secret_id": "", "secret_key": "", "shortcut_char": "z"}

    def save_config(self):
        data = {"secret_id": self.secret_id, "secret_key": self.secret_key, "shortcut_char": self.current_char}
        try:
            with open(CONFIG_FILE, 'w') as f: json.dump(data, f, indent=4)
        except: pass

    def quit_app(self, icon, item):
        self.icon.stop()
        self.root.quit()
        sys.exit()

if __name__ == '__main__':
    app = WeChatTranslator()
    app.root.mainloop()
#!/usr/bin/env python3
import subprocess, sys, time
from pathlib import Path
from pna_ui_common import *

MODE_DIR = Path(__file__).resolve().parent
ROOT = MODE_DIR.parent
PYTHON = sys.executable

MODES=[
    ("PNA ANALYZER","Wi-Fi • BLE • RF • Ethernet",[PYTHON, str(ROOT / "pna.py")],ACCENT),
    ("BRUCE SAFE","Passive wireless security monitor",[PYTHON, str(MODE_DIR / "bruce_safe.py")],GREEN),
    ("SECURITY LAB","Safe event simulation & detection demo",[PYTHON, str(MODE_DIR / "security_lab.py")],YELLOW),
    ("SHUTDOWN","Safely power off the PNA",None,RED),
]
AUTO=10.0

class Launcher:
    def __init__(self):
        self.display=None; self.enc=None
        self.sel=0; self.touched=False; self.started=time.monotonic()

    def open_hw(self):
        self.display=ST7796S()
        self.enc=EncoderPair(4)

    def close_hw(self):
        if self.enc:
            try:self.enc.close()
            except Exception:pass
            self.enc=None
        if self.display:
            try:self.display.close()
            except Exception:pass
            self.display=None
        time.sleep(0.2)

    def draw(self):
        image,draw=new_frame()
        remain=max(0,int(AUTO-(time.monotonic()-self.started)))
        header(draw,"SYSTEM SELECT","AUTO PNA" if self.touched else f"AUTO {remain}s")
        text(draw,(24,54),"Choose operating mode",FONT_MD,TEXT)
        y=84
        for i,(name,sub,cmd,color) in enumerate(MODES):
            selected=i==self.sel
            panel(draw,(20,y,460,y+45),PANEL2 if selected else PANEL,color if selected else None,8)
            if selected: draw.rectangle((20,y,25,y+45),fill=color)
            text(draw,(37,y+13),name,FONT_TITLE,color if selected else TEXT)
            text(draw,(220,y+14),sub,FONT_SM,MUTED)
            y+=51
        footer(draw,"L rotate: select   L click: launch   Hold L 5s: power")
        self.display.show(image)

    def launch(self):
        name,_,cmd,_=MODES[self.sel]
        if name=="SHUTDOWN":
            self.close_hw()
            subprocess.run(["/usr/bin/sudo","/usr/bin/systemctl","poweroff"],check=False)
            return False
        self.close_hw()
        subprocess.run(cmd,cwd=ROOT,check=False)
        self.started=time.monotonic(); self.touched=True
        self.open_hw()
        return True

    def run(self):
        self.open_hw(); last=0
        try:
            while True:
                now=time.monotonic()
                for e in self.enc.poll():
                    if e in ("LEFT_CW","LEFT_CCW","LEFT_PRESS"): self.touched=True
                    if e=="LEFT_CW": self.sel=(self.sel+1)%len(MODES)
                    elif e=="LEFT_CCW": self.sel=(self.sel-1)%len(MODES)
                    elif e=="LEFT_PRESS":
                        if not self.launch(): return
                    elif e=="LEFT_HOLD":
                        self.sel=len(MODES)-1
                        if not self.launch(): return
                if not self.touched and now-self.started>=AUTO:
                    self.sel=0; self.launch()
                if now-last>=0.20:
                    self.draw(); last=now
                time.sleep(0.002)
        finally:
            self.close_hw()

if __name__=="__main__":
    Launcher().run()

#!/usr/bin/env python3
import subprocess,time
from collections import deque
from pna_ui_common import *

SCENARIOS=[
    ("DEAUTH BURST","Simulated management-frame burst"),
    ("BEACON FLOOD","Simulated abnormal beacon rate"),
    ("ROGUE AP","Simulated duplicate SSID anomaly"),
    ("CHANNEL CONGESTION","Simulated high RF activity"),
    ("BLE BURST","Simulated dense BLE advertisements"),
]

class SecurityLab:
    def __init__(self):
        self.display=ST7796S();self.enc=EncoderPair(4)
        self.sel=0;self.events=deque(maxlen=6);self.last=0
    def close(self):
        try:self.enc.close()
        except Exception:pass
        try:self.display.close()
        except Exception:pass
    def trigger(self):
        self.events.appendleft((time.time(),SCENARIOS[self.sel][0]))
    def draw(self):
        image,draw=new_frame();header(draw,"SECURITY LAB","SIMULATION")
        text(draw,(18,48),"Safe detection demo",FONT_TITLE,YELLOW)
        y=77
        for i,(name,sub) in enumerate(SCENARIOS):
            sel=i==self.sel
            panel(draw,(18,y,462,y+38),PANEL2 if sel else PANEL,YELLOW if sel else None,7)
            text(draw,(30,y+11),name,FONT_SM,YELLOW if sel else TEXT)
            text(draw,(214,y+11),sub,FONT_SM,MUTED)
            y+=43
        panel(draw,(18,260,462,289),PANEL2)
        if self.events:
            age=int(time.time()-self.events[0][0])
            text(draw,(30,275),f"Last simulation: {self.events[0][1]} ({age}s ago)",FONT_SM,GREEN,anchor="lm")
        else:
            text(draw,(30,275),"Rotate to select • Press LEFT to trigger simulation",FONT_SM,MUTED,anchor="lm")
        footer(draw,"L rotate: select   L click: trigger   Hold R 3s: selector")
        self.display.show(image)
    def handle(self,e):
        if e=="LEFT_CW":self.sel=(self.sel+1)%len(SCENARIOS)
        elif e=="LEFT_CCW":self.sel=(self.sel-1)%len(SCENARIOS)
        elif e=="LEFT_PRESS":self.trigger()
        elif e=="RIGHT_HOLD":raise SystemExit(0)
        elif e=="LEFT_HOLD":
            self.close()
            subprocess.run(["/usr/bin/sudo","/usr/bin/systemctl","poweroff"],check=False)
            raise SystemExit(0)
    def run(self):
        try:
            while True:
                for e in self.enc.poll():self.handle(e)
                now=time.monotonic()
                if now-self.last>=0.20:self.draw();self.last=now
                time.sleep(0.002)
        finally:self.close()

if __name__=="__main__":
    SecurityLab().run()

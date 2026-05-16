import pystray
from PIL import Image, ImageDraw


def create_icon():
    img = Image.new("RGB", (64, 64), color=(30, 30, 30))
    draw = ImageDraw.Draw(img)
    draw.ellipse([8, 8, 56, 56], fill=(255, 165, 0))
    draw.text((20, 22), "C", fill=(0, 0, 0))
    return img


def on_quit(icon, item):
    icon.stop()


def main():
    icon = pystray.Icon(
        name="ClaudeMonitor",
        icon=create_icon(),
        title="Hello World",
        menu=pystray.Menu(
            pystray.MenuItem("Hello World", lambda icon, item: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        ),
    )
    icon.run()


if __name__ == "__main__":
    main()

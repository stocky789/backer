#!/usr/bin/env python3
"""Generate the Backer application icon (ICO file) for Windows."""

from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("PIL/Pillow is required. Install with: pip install Pillow")
    exit(1)


def create_icon_image(size: int) -> Image.Image:
    """
    Create the Backer icon at the specified size.

    Design: Shield shape with stacked blocks (representing backup layers)
    and a green checkmark badge.
    """
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Scale factor for drawing
    s = size / 64  # Base design is 64x64

    # Colors (matching the SVG)
    bg_dark = (26, 35, 50)       # #1a2332
    bg_inner = (13, 17, 23)      # #0d1117
    accent = (88, 166, 255)      # #58a6ff
    success = (63, 185, 80)      # #3fb950
    white = (255, 255, 255)

    # Draw shield shape (simplified polygon for small sizes)
    # Shield points: top center, bottom corners, bottom center point
    shield_outer = [
        (32 * s, 4 * s),    # Top center
        (8 * s, 14 * s),    # Top left
        (8 * s, 32 * s),    # Left side
        (32 * s, 52 * s),   # Bottom center
        (56 * s, 32 * s),   # Right side
        (56 * s, 14 * s),   # Top right
    ]

    # Draw outer shield
    draw.polygon(shield_outer, fill=bg_dark, outline=accent)

    # Draw inner shield (slightly smaller)
    shield_inner = [
        (32 * s, 10 * s),   # Top center
        (14 * s, 18 * s),   # Top left
        (14 * s, 30 * s),   # Left side
        (32 * s, 44 * s),   # Bottom center
        (50 * s, 30 * s),   # Right side
        (50 * s, 18 * s),   # Top right
    ]
    draw.polygon(shield_inner, fill=bg_inner)

    # Draw stacked blocks (representing backup layers)
    # Scale block positions and sizes
    blocks = [
        (22 * s, 18 * s, 38 * s, 24 * s),  # Top block
        (22 * s, 27 * s, 42 * s, 33 * s),  # Middle block
        (22 * s, 36 * s, 40 * s, 42 * s),  # Bottom block
    ]

    for x1, y1, x2, y2 in blocks:
        draw.rectangle([x1, y1, x2, y2], fill=accent)

    # Vertical bar
    draw.rectangle([22 * s, 18 * s, 27 * s, 42 * s], fill=accent)

    # Draw green checkmark circle badge
    cx, cy, r = 44 * s, 44 * s, 8 * s
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=success)

    # Draw checkmark (for sizes 32 and above for visibility)
    if size >= 32:
        # Checkmark path: M40 44l3 3 5-6 (from SVG)
        check_points = [
            (40 * s, 44 * s),
            (43 * s, 47 * s),
            (48 * s, 41 * s),
        ]
        draw.line(check_points, fill=white, width=max(1, int(2 * s)))

    return img


def main():
    """Generate the ICO file with multiple sizes."""
    # Sizes commonly used in Windows ICO files
    sizes = [16, 24, 32, 48, 64, 128, 256]

    # Generate images at each size
    images = []
    for size in sizes:
        img = create_icon_image(size)
        images.append(img)
        print(f"Generated {size}x{size} icon")

    # Output path - in the agent/gui directory where the app looks for it
    script_dir = Path(__file__).parent
    output_dir = script_dir.parent / "src" / "backer" / "agent" / "gui"
    output_path = output_dir / "backer.ico"

    # For ICO format, we save using the largest image as base
    # and specify sizes to include in the file
    largest = images[-1]  # 256x256
    largest.save(
        output_path,
        format='ICO',
        sizes=[(s, s) for s in sizes],
    )

    print(f"\nIcon saved to: {output_path}")

    # Verify the saved file
    saved = Image.open(output_path)
    print(f"Saved ICO size: {saved.size}, frames: {getattr(saved, 'n_frames', 1)}")


if __name__ == '__main__':
    main()

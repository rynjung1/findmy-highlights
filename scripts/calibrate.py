"""One-time plate-zone calibration for a camera setup.

The fusion pipeline needs to know where home plate is in the frame. Run
this once per camera position (not per video — clips from the same mounted
camera share one calibration):

    # interactive: opens a window, click on home plate, press 's' to save
    python scripts/calibrate.py reference_clips/clip_60.mkv

    # non-interactive: pass pixel coordinates directly
    python scripts/calibrate.py reference_clips/clip_60.mkv --set 1147,840

The calibration is saved as calibration.json next to the video (override
with --output) and applies to every video processed from that directory.
The zone radius defaults to 26% of frame height (roughly one batter-height
around the plate at typical backstop-mounted framing); override with
--radius if the camera is unusually near/far.
"""

import argparse
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def grab_frame(video_path: str, at_frac: float = 0.5):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        sys.exit(f"could not open video: {video_path}")
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * at_frac))
    ok, frame = cap.read()
    if not ok:  # fall back to first frame
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, frame = cap.read()
    cap.release()
    if not ok:
        sys.exit(f"could not read a frame from: {video_path}")
    return frame


def pick_interactively(frame, radius):
    clicked = []
    disp_h = 800
    scale = disp_h / frame.shape[0]
    disp_size = (int(frame.shape[1] * scale), disp_h)

    def redraw():
        img = cv2.resize(frame, disp_size)
        if clicked:
            x, y = clicked[-1]
            cv2.circle(img, (int(x * scale), int(y * scale)), 6, (0, 0, 255), -1)
            cv2.circle(img, (int(x * scale), int(y * scale)),
                       int(radius * scale), (0, 255, 0), 2)
        cv2.putText(img, "click home plate, then press 's' to save, 'q' to abort",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        cv2.imshow("calibrate", img)

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            clicked.append((x / scale, y / scale))
            redraw()

    cv2.namedWindow("calibrate")
    cv2.setMouseCallback("calibrate", on_mouse)
    redraw()
    while True:
        key = cv2.waitKey(50) & 0xFF
        if key == ord("s") and clicked:
            cv2.destroyAllWindows()
            return clicked[-1]
        if key == ord("q"):
            cv2.destroyAllWindows()
            sys.exit("calibration aborted")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--set", dest="set_xy", metavar="X,Y",
                    help="plate pixel coordinates (skips the interactive window)")
    ap.add_argument("--radius", type=float, default=None,
                    help="zone radius in pixels (default: 26%% of frame height)")
    ap.add_argument("--output", default=None,
                    help="output path (default: calibration.json next to video)")
    args = ap.parse_args()

    frame = grab_frame(args.video)
    h, w = frame.shape[:2]
    radius = args.radius if args.radius is not None else 0.26 * h

    if args.set_xy:
        x, y = (float(v) for v in args.set_xy.split(","))
    else:
        x, y = pick_interactively(frame, radius)

    out = Path(args.output) if args.output else Path(args.video).parent / "calibration.json"
    out.write_text(json.dumps({
        "frame_size": [w, h],
        "plate_xy": [round(x, 1), round(y, 1)],
        "zone_radius_px": round(radius, 1),
        "created_from": Path(args.video).name,
    }, indent=2))
    print(f"saved {out}: plate=({x:.0f},{y:.0f}) radius={radius:.0f}px "
          f"frame={w}x{h}")


if __name__ == "__main__":
    main()

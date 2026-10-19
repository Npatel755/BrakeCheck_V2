"""Build a contact sheet to identify camera footage in screen recordings."""

import argparse
from pathlib import Path

import cv2
import numpy as np


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("videos", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tiles = []
    for video in args.videos:
        cap = cv2.VideoCapture(str(video))
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        for fraction in (0.1, 0.3, 0.5, 0.7, 0.9):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(count * fraction))
            ok, frame = cap.read()
            if not ok:
                continue
            thumb = cv2.resize(frame, (212, 424) if frame.shape[0] > frame.shape[1] else (424, 212))
            tile = np.zeros((460, 440, 3), dtype=np.uint8)
            y = (440 - thumb.shape[0]) // 2
            x = (440 - thumb.shape[1]) // 2
            tile[y:y + thumb.shape[0], x:x + thumb.shape[1]] = thumb
            cv2.putText(tile, f"{video.name} {fraction:.0%}", (8, 452), cv2.FONT_HERSHEY_SIMPLEX, .58, (255, 255, 255), 1)
            tiles.append(tile)
        cap.release()
    if not tiles:
        raise ValueError("No frames decoded")
    rows = [np.hstack(tiles[i:i + 5]) for i in range(0, len(tiles), 5)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(args.output), np.vstack(rows))


if __name__ == "__main__":
    main()

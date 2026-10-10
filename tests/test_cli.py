"""--camera の指定の解釈と、配信するカメラの決め方のテスト。カメラは開かない。"""

from __future__ import annotations

import unittest

from kasumin.cli import parse_camera_spec, plan_cameras
from kasumin.devices import VideoDevice

DEVICES = [
    VideoDevice(0, "Integrated Webcam", r"\\?\usb#vid_0001&pid_0001#a"),
    VideoDevice(1, "HD Pro Webcam C920", r"\\?\usb#vid_046d&pid_082d#b"),
    VideoDevice(2, "HD Pro Webcam C920", r"\\?\usb#vid_046d&pid_082d#c"),
    VideoDevice(3, "OBS Virtual Camera", ""),
]


class ParseCameraSpecTest(unittest.TestCase):
    def test_number_only(self):
        self.assertEqual(parse_camera_spec("1"), (1, {}))
        self.assertEqual(parse_camera_spec(" 12 "), (12, {}))

    def test_per_camera_options(self):
        index, options = parse_camera_spec(
            "1, name = 玄関 ,rotate=180,flip=H,fourcc=mjpg,quality=70,width=640,height=480,fps=15"
        )
        self.assertEqual(index, 1)
        self.assertEqual(
            options,
            {
                "name": "玄関",
                "rotate": 180,
                "flip": "h",
                "fourcc": "MJPG",
                "jpeg_quality": 70,
                "width": 640,
                "height": 480,
                "fps": 15.0,
            },
        )

    def test_all(self):
        self.assertEqual(parse_camera_spec("all"), ("all", {}))
        self.assertEqual(parse_camera_spec("ALL"), ("all", {}))

    def test_invalid_specs(self):
        for spec in (
            "",
            "front",
            "-1",
            "100",
            "１",
            "1,foo=1",
            "1,name",
            "1,name=",
            "1,rotate=45",
            "1,flip=x",
            "1,quality=0",
            "1,fourcc=MJPEG",
            "1,width=-5",
            "1,width=abc",
            "1,fps=nan",
            "1,fps=inf",
            "1,rotate=90,rotate=180",
            "all,width=640",
        ):
            with self.subTest(spec), self.assertRaises(ValueError):
                parse_camera_spec(spec)

    def test_error_message_names_the_bad_value(self):
        with self.assertRaises(ValueError) as ctx:
            parse_camera_spec("1,width=abc")
        self.assertIn("width=abc", str(ctx.exception))
        self.assertIn("数値", str(ctx.exception))


class PlanCamerasTest(unittest.TestCase):
    def test_default_is_camera_0_without_tracking(self):
        # 1 台だけのときは従来どおり番号で開く (抜き差しの追跡はしない)
        options = {"name": "Integrated Webcam", "key": "path:" + DEVICES[0].path}
        self.assertEqual(plan_cameras(None, DEVICES), [(0, options)])

    def test_settings_key_follows_the_device(self):
        # 設定画面で変えた値の保存先。台数や番号が変わっても同じカメラに付くよう、接続先を優先する
        plan = dict(plan_cameras(["1", "3"], DEVICES))
        self.assertEqual(plan[1]["key"], "path:" + DEVICES[1].path)
        self.assertEqual(plan[3]["key"], "name:OBS Virtual Camera")
        self.assertEqual(dict(plan_cameras(["1"], DEVICES))[1]["key"], plan[1]["key"], "1 台でも同じキー")
        twins = [VideoDevice(0, "Same", ""), VideoDevice(1, "Same", "")]
        self.assertNotIn("key", dict(plan_cameras(["0"], twins))[0], "区別できなければ番号で覚える")

    def test_several_cameras_are_tracked_by_device_path(self):
        plan = plan_cameras(["0", "1"], DEVICES)
        self.assertEqual([index for index, _ in plan], [0, 1])
        self.assertEqual(plan[0][1]["device_path"], DEVICES[0].path)
        self.assertEqual(plan[1][1]["device_path"], DEVICES[1].path)

    def test_same_model_twice_gets_numbered_names(self):
        plan = plan_cameras(["1", "2"], DEVICES)
        self.assertEqual([options["name"] for _, options in plan], ["HD Pro Webcam C920 (1)", "HD Pro Webcam C920 (2)"])

    def test_camera_without_path_is_tracked_by_unique_name(self):
        plan = dict(plan_cameras(["0", "3"], DEVICES))
        self.assertEqual(plan[3]["device_name"], "OBS Virtual Camera")
        self.assertNotIn("device_path", plan[3])

    def test_given_name_and_options_are_kept(self):
        plan = dict(plan_cameras(["0,name=リビング", "1,rotate=180"], DEVICES))
        self.assertEqual(plan[0]["name"], "リビング")
        self.assertEqual((plan[1]["name"], plan[1]["rotate"]), ("HD Pro Webcam C920", 180))

    def test_all_uses_every_connected_camera(self):
        self.assertEqual([index for index, _ in plan_cameras(["all"], DEVICES)], [0, 1, 2, 3])
        # 1 台も見つからなければ、従来どおりカメラ 0 の接続を待つ
        self.assertEqual(plan_cameras(["all"], []), [(0, {})])

    def test_invalid_combinations(self):
        for values, devices in (
            (["all"], None),  # 列挙できない環境
            (["all", "0"], DEVICES),
            (["1", "1"], DEVICES),
            (["1,name=a", "1,name=b"], DEVICES),
        ):
            with self.subTest(values), self.assertRaises(ValueError):
                plan_cameras(values, devices)

    def test_without_device_list(self):
        self.assertEqual(plan_cameras(["0", "1"], None), [(0, {}), (1, {})])

    def test_missing_camera_is_opened_by_number(self):
        plan = dict(plan_cameras(["0", "5"], DEVICES))
        self.assertEqual(plan[5], {})


if __name__ == "__main__":
    unittest.main()

"""Запуск поиска драйвера.

    python3 -m drvsearch --vid 0bda --pid 8179          поиск в консоли
    python3 -m drvsearch --vid 0bda --pid 8179 --json   результат в JSON
    python3 -m drvsearch --vid 0bda --pid 8179 --no-llm без языковой модели
    python3 -m drvsearch --window --in-fd R --out-fd W  окно (из главного окна)
    python3 -m drvsearch --setup-model                  проверить и скачать модель
    python3 -m drvsearch --setup-model --retest         проверить модель заново
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def device_from_ids(vid, pid, name=None):
    """Минимальные сведения об устройстве по VID:PID и базе usb.ids."""
    from usbmon.usbids import UsbIds
    from usbmon.devices import join_names
    ids = UsbIds.load()
    vendor = ids.vendor_name(vid) or ""
    product = ids.product_name(vid, pid) or ""
    db_name = join_names(vendor, product)
    return {"vid": vid, "pid": pid, "vendor_db": vendor, "product_db": product,
            "manufacturer": "", "product": name or "",
            "name_from_device": name or "", "name_from_db": db_name,
            "full_name": name or db_name or "%04x:%04x" % (vid, pid),
            "verdict": "missing", "interfaces": []}


def setup_model(model, retest=False):
    """Полная подготовка модели в консоли (то же делает главное окно при запуске)."""
    from .runtime import (Cancelled, ModelSetup, SelfTestFailed, SetupError,
                          WeakHardware, human)
    state = {"stage": None, "shown": -1}

    def progress(stage, text, done, total):
        if stage == "ready":                  # итог печатается ниже
            return
        if done is not None and total:
            percent = int(done * 100 / total)
            if stage != state["stage"]:
                print("..", text, "(%s)" % human(total), flush=True)
            if percent != state["shown"]:
                print("\r   %3d%%  %s из %s " % (percent, human(done), human(total)),
                      end="", flush=True)
            state["shown"] = percent
        else:
            if state["shown"] >= 0:
                print()
            state["shown"] = -1
            print("..", text, flush=True)
        state["stage"] = stage

    def report():
        print("\nЧто проверено:")
        for line in setup.report:
            print("  " + line)

    setup = ModelSetup(model, progress=progress, retest=retest)
    try:
        url = setup.run()
    except KeyboardInterrupt:
        print("\nОстановлено. Загрузка продолжится с того же места при следующем запуске.")
        return 130
    except Cancelled as exc:
        print("\n" + str(exc))
        return 130
    except WeakHardware as exc:
        print("\nМодель отключена: %s." % exc)
        if not isinstance(exc, SelfTestFailed):
            print("\n".join(setup.hw.summary()))
        report()
        print("Поиск драйверов работает и без модели.")
        return 3
    except SetupError as exc:
        print("\nОшибка: %s" % exc, file=sys.stderr)
        report()
        return 1
    finally:
        setup.stop_server()      # служба нужна только на время работы приложения
    report()
    print("\nМодель %s готова (служба Ollama: %s)." % (setup.model, url))
    print("Ollama и модель хранятся в папке", setup.base)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m drvsearch",
                                     description="Поиск драйвера USB-устройства на GitHub")
    parser.add_argument("--vid", help="идентификатор производителя, hex")
    parser.add_argument("--pid", help="идентификатор изделия, hex")
    parser.add_argument("--name", help="название устройства")
    parser.add_argument("--device-json", help="файл со сведениями об устройстве")
    parser.add_argument("--no-llm", action="store_true", help="не использовать модель")
    parser.add_argument("--model", help="модель Ollama (по умолчанию qwen2.5:1.5b)")
    parser.add_argument("--retest", action="store_true",
                        help="с --setup-model: провести самопроверку модели заново")
    parser.add_argument("--setup-model", action="store_true",
                        help="проверить модель и скачать её при отсутствии")
    parser.add_argument("--json", action="store_true", help="вывести результат в JSON")
    parser.add_argument("--window", action="store_true", help="открыть окно")
    parser.add_argument("--in-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--out-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.model:
        os.environ["SUPERAPP_LLM_MODEL"] = args.model
    if args.setup_model:
        return setup_model(args.model, args.retest)

    device = None
    if args.device_json:
        with open(args.device_json, encoding="utf-8") as f:
            device = json.load(f)
    elif args.vid and args.pid:
        device = device_from_ids(int(args.vid, 16), int(args.pid, 16), args.name)

    if args.window:
        from .window import main as window_main
        return window_main(args.in_fd, args.out_fd, device, not args.no_llm)

    if device is None:
        parser.error("укажите --vid и --pid или --device-json")
    from .pipeline import DriverSearch
    from .report import to_text
    from .runtime import locate
    progress = (lambda t: None) if args.json else (lambda t: print("..", t, flush=True))
    # уже скачанная модель: найти работающую службу или запустить её на время поиска
    setup = None if args.no_llm else locate(args.model)
    try:
        result = DriverSearch(use_llm=not args.no_llm, progress=progress).run(device)
    finally:
        if setup is not None:
            setup.stop_server()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print()
        print(to_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

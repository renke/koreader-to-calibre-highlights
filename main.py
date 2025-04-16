import argparse
import base64
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import calibre.library
from calibre.ebooks.oeb.polish.container import get_container

from slpp import slpp as lua


def main(koreader_calibre_metadata_path, calibre_library_path):
    calibre_db = calibre.library.db(calibre_library_path).new_api

    with open(koreader_calibre_metadata_path, "r") as file:
        koreader_books = json.load(file)

    # TODO: Add try/except and collect failed books to we can print them at the end
    for koreader_book in koreader_books:
        calibre_book_id = koreader_book["application_id"]

        print(f"📚 Processing book: {koreader_book['title']}")

        if not calibre_db.has_id(calibre_book_id):
            print(f"⚠️ Book with ID '{calibre_book_id}' not found in Calibre")
            continue

        koreader_sidecar_path = (
            Path(koreader_calibre_metadata_path)
            .parent.joinpath(koreader_book["lpath"])
            .with_suffix(".sdr")
            .joinpath("metadata.epub.lua")
        )

        if not koreader_sidecar_path.exists():
            print(f"ℹ️ No sidecar file found for book")
            continue

        with open(koreader_sidecar_path, "r") as file:
            koreader_sidecar = lua.decode(re.sub("^[^{]*", "", file.read()).strip())

        if not koreader_sidecar.get("annotations"):
            print(f"ℹ️ No annotations for book {calibre_book_id}")
            continue

        koreader_highlights = list(koreader_sidecar["annotations"].values())

        if not koreader_highlights:
            print(f"ℹ️ No highlights found")
            continue

        calibre_book_path = calibre_db.format_abspath(calibre_book_id, "EPUB")

        calibre_book_container = get_container(calibre_book_path, tweak_mode=True)

        calibre_highlights = [
            koreader_highlight_to_calibre(koreader_highlight, calibre_book_container)
            for koreader_highlight in koreader_highlights
            if koreader_highlight is not None
        ]

        calibre_highlights = [
            calibre_highlight
            for calibre_highlight in calibre_highlights
            if calibre_highlight is not None
        ]

        if not calibre_highlights:
            print(f"ℹ️ No highlights found")
            continue

        print(f"✅ Found {len(calibre_highlights)} highlights")

        existing_calibre_db_annotations = [
            a
            for a in calibre_db.all_annotations(
                ignore_removed=True,
                restrict_to_book_ids=[calibre_book_id],
            )
            if a["format"] == "EPUB"
        ]

        def same_calibre_highlight(a, b):
            return (
                a["start_cfi"] == b["start_cfi"]
                and a["end_cfi"] == b["end_cfi"]
                and a["spine_index"] == b["spine_index"]
            )

        calibre_db_annotation_to_delete = existing_calibre_db_annotations[:]

        calibre_highlights_to_merge = []

        for calibre_highlight in calibre_highlights:
            exists = False

            for existing_calibre_db_annotation in existing_calibre_db_annotations:
                existing_calibre_highlight = existing_calibre_db_annotation[
                    "annotation"
                ]

                if same_calibre_highlight(
                    calibre_highlight, existing_calibre_highlight
                ):
                    calibre_highlights_to_merge.append(
                        {
                            **calibre_highlight,
                            # Make sure we use the existing UUID
                            "uuid": existing_calibre_db_annotation["annotation"][
                                "uuid"
                            ],
                        }
                    )

                    calibre_db_annotation_to_delete.remove(
                        existing_calibre_db_annotation
                    )

                    exists = True
                    break

            if exists:
                continue

            calibre_highlights_to_merge.append(calibre_highlight)

        # print("delete db id", [a["id"] for a in calibre_db_annotation_to_delete])
        # print("merge highlight id", [a["uuid"] for a in calibre_highlights_to_merge])

        calibre_db.delete_annotations(
            [a["id"] for a in calibre_db_annotation_to_delete],
        )

        calibre_db.merge_annotations_for_book(
            calibre_book_id,
            "EPUB",
            calibre_highlights_to_merge,
        )

        # calibre_db.set_annotations_for_book(
        #     calibre_book_id,
        #     "EPUB",
        #     # calibre_highlights,
        #     [],
        # )


def koreader_highlight_to_calibre(koreader_highlight, calibre_book_container):
    pos0 = koreader_highlight.get("pos0")
    pos1 = koreader_highlight.get("pos1")

    if pos0 is None:
        return None

    if pos1 is None:
        return None

    (calibre_start_cfi, start_spine_index, start_spine_name) = (
        koreader_pos_to_calibre_cfi(pos0, calibre_book_container)
    )

    (calibre_end_cfi, end_spine_index, _) = koreader_pos_to_calibre_cfi(
        pos1, calibre_book_container
    )

    # Highlights across multiple spines are not supported in Calibre. We could split them into multiple highlights, but that's way too much work.
    if start_spine_index != end_spine_index:
        return None

    calibre_highlight = {
        "type": "highlight",
        "start_cfi": calibre_start_cfi,
        "end_cfi": calibre_end_cfi,
        "spine_index": start_spine_index,
        "spine_name": start_spine_name,
        # Koreader (or maybe Lua?) adds an \ before every newline, which is not needed in Calibre.
        "highlighted_text": re.sub(r"\\\n", r"\n", koreader_highlight["text"]),
        "uuid": base64.urlsafe_b64encode(os.urandom(16)).decode("utf-8")[:22],
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z",
        "style": koreader_style_to_calibre_style(
            koreader_highlight["drawer"], koreader_highlight["color"]
        ),
        # TODO: Handle subchapters (but first test what calibre does with them)
        "toc_family_titles": [koreader_highlight["chapter"]],
    }

    return calibre_highlight


def koreader_pos_to_calibre_cfi(koreader_pos, calibre_book_container):
    koreader_pos_match = re.search(
        r"/body/DocFragment\[(\d+)\](.*)/text\(\)(?:\[(\d+)\])?\.(\d+)", koreader_pos
    )

    spine_index = int(koreader_pos_match.group(1)) - 1

    koreader_xpath = koreader_pos_match.group(2)

    # TODO: This need to be used somehow!!!
    koreader_text_index = (
        int(koreader_pos_match.group(3))
        if koreader_pos_match.group(3) is not None
        else None
    )

    koreader_offset = int(koreader_pos_match.group(4))

    spine_name = [spine_item[0] for spine_item in calibre_book_container.spine_names][
        spine_index
    ]

    koreader_xpath_without_ns = "/html" + koreader_xpath

    koreader_xpath_with_ns = "/ns:html"

    for part in koreader_xpath.strip("/").split("/"):
        if part == "text()":
            koreader_xpath_with_ns += ""
        else:
            koreader_xpath_with_ns += f"/ns:{part}"

    spine_html = calibre_book_container.parsed(spine_name)

    target_element = spine_html.xpath(
        koreader_xpath_with_ns, namespaces={"ns": "http://www.w3.org/1999/xhtml"}
    )[0]

    calibre_cfi = get_calibre_cfi_with_ids_and_offset(
        target_element, offset=koreader_offset, text_index=koreader_text_index
    )

    return (calibre_cfi, spine_index, spine_name)


# Generated by LLM ;)
def koreader_style_to_calibre_style(koreader_drawer: str, koreader_color: str) -> dict:
    color_map = {
        "red": "red",
        "orange": "yellow",
        "yellow": "yellow",
        "green": "green",
        "olive": "green",
        "cyan": "blue",
        "blue": "blue",
        "purple": "purple",
        "gray": "yellow",
    }

    decoration_map = {
        "lighten": None,
        "underscore": "wavy",
        "strikeout": "strikeout",
        "invert": "strikeout",
    }

    if koreader_drawer in decoration_map and decoration_map[koreader_drawer]:
        return {
            "style": {
                "kind": "decoration",
                "type": "builtin",
                "which": decoration_map[koreader_drawer],
            }
        }

    return {
        "kind": "color",
        "type": "builtin",
        "which": color_map.get(koreader_color, "yellow"),
    }


# Generated by LLM ;)
def get_first_text_descendant(element):
    current = element
    while True:
        children = [
            e for e in current if isinstance(e.tag, str)
        ]  # Filter out comments/PIs
        if not children:
            return current  # No more children: return last valid
        first = children[0]
        if current.text is not None and current.text.strip():
            return current  # Text before first child: stop and return current
        current = first  # Descend into first child


# Generated by LLM ;)
def get_last_text_descendant(element):
    stack = [element]
    last_with_text = None
    last_element = element  # fallback: keep track of deepest visited

    while stack:
        current = stack.pop()
        last_element = current  # update fallback
        if current.text and current.text.strip():
            last_with_text = current
        # Traverse children in document order
        children = [e for e in reversed(current) if isinstance(e.tag, str)]
        stack.extend(children)

    return last_with_text if last_with_text is not None else last_element


# Generated by LLM ;)
def get_calibre_cfi_with_ids_and_offset(raw_element, offset, text_index):
    element = get_first_text_descendant(raw_element) if offset == 0 else raw_element

    steps = []

    def walk_parents():
        current_element = element

        while current_element.getparent() is not None:
            parent = current_element.getparent()

            siblings = [
                e for e in parent if isinstance(e.tag, str)
            ]  # Only element nodes

            index = siblings.index(current_element)  # 0-based index

            cfi_step = 2 * (index + 1)  # 1-based index, multiplied by 2

            element_id = current_element.get("id")

            step = f"{cfi_step}[{element_id}]" if element_id else str(cfi_step)

            steps.insert(0, step)

            current_element = parent

    walk_parents()

    steps.insert(0, "2")

    def get_texts(element):
        texts = []

        if element.text:
            texts.append(element.text)

        for child in element:
            if child.tail:
                texts.append(child.tail)

        return texts

    def get_text_and_elements(element):
        texts_and_children = []

        if element.text:
            texts_and_children.append((element.text, None))

        for child in element:
            if child.tail:
                texts_and_children.append((child.tail, child))

        return texts_and_children

    text_elements = get_texts(element)

    # here be dragons AKA I should clean this up
    if text_index is not None and text_index > 1 and offset == 0:
        text_and_elements = get_text_and_elements(element)

        if (text_and_elements[text_index - 1]) is None:
            actual_offset = (
                len("".join(text_elements[: text_index - 1])) + offset
                if text_index is not None
                else offset
            )

            steps.append(f"1:{actual_offset}")
        else:
            island_element = text_and_elements[text_index - 1][1]

            actual_element = get_last_text_descendant(island_element)

            if actual_element.text is None:
                actual_offset = (
                    len("".join(text_elements[: text_index - 1])) + offset
                    if text_index is not None
                    else offset
                )

                steps.append(f"1:{actual_offset}")
            else:
                actual_offset = offset + len(actual_element.text)

                return get_calibre_cfi_with_ids_and_offset(
                    actual_element, actual_offset, None
                )
    else:
        actual_offset = (
            len("".join(text_elements[: text_index - 1])) + offset
            if text_index is not None
            else offset
        )

        steps.append(f"1:{actual_offset}")

    return "/" + "/".join(steps)


parser = argparse.ArgumentParser(description="Import KOReader highlights into Calibre")

parser.add_argument(
    "koreader_calibre_metadata_path",
    help="Path to .metadata.calibre on your KOReader device",
)
parser.add_argument("calibre_library_path", help="Path to your Calibre library")

args = parser.parse_args()

koreader_calibre_metadata_path = args.koreader_calibre_metadata_path
calibre_library_path = args.calibre_library_path

main(
    koreader_calibre_metadata_path=koreader_calibre_metadata_path,
    calibre_library_path=calibre_library_path,
)

import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from stat import S_ISLNK
from typing import Literal
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


class EmployeePackageValidationFailure(ValueError):
    def __init__(self, code: str, **details: object) -> None:
        super().__init__(code)
        self.details = details


@dataclass(frozen=True)
class EmployeeProductImage:
    filename: str
    role: Literal["main", "detail"]


@dataclass(frozen=True)
class EmployeeSpreadsheetProduct:
    main_image_reference: str
    product_name: str = ""
    material_label: str = ""
    product_form: str = ""
    visual_observations: tuple[str, ...] = ()
    selling_points: tuple[str, ...] = ()
    suggested_price: str = ""


@dataclass(frozen=True)
class EmployeeProductImageGroup:
    product_code: str
    sequence: int
    images: tuple[EmployeeProductImage, ...]
    product_name: str = ""
    material_label: str = ""
    product_form: str = ""
    visual_observations: tuple[str, ...] = ()
    selling_points: tuple[str, ...] = ()
    suggested_price: str = ""


@dataclass(frozen=True)
class EmployeePackageValidationResult:
    template_file: str
    image_count: int
    products: tuple[EmployeeProductImageGroup, ...]
    workbook_count: int = 0
    video_count: int = 0
    filename_warnings: tuple[dict[str, str], ...] = ()
    spreadsheet_warning_zh: str = ""
    next_step_zh: str = "资料包结构正确，可以进入图片审核。"

    @property
    def product_count(self) -> int:
        return len(self.products)


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi"}
SPREADSHEET_NAMESPACE = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
PRODUCT_COLUMN_HEADERS = {
    "主图文件名（必填）": "main_image_reference",
    "商品名称（选填）": "product_name",
    "材质或品类（选填）": "material_label",
    "商品形态（选填）": "product_form",
    "可见特点（选填）": "visual_observations",
    "销售要点（选填）": "selling_points",
    "建议价格区间（选填）": "suggested_price",
}


def _cell_column(reference: str) -> str:
    return "".join(character for character in reference if character.isalpha())


def _shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(text.text or "" for text in item.iter(f"{SPREADSHEET_NAMESPACE}t")) for item in root
    ]


def _cell_text(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.get("t")
    value = cell.findtext(f"{SPREADSHEET_NAMESPACE}v", default="")
    if cell_type == "s" and value.isdigit():
        return shared_strings[int(value)]
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.iter(f"{SPREADSHEET_NAMESPACE}t"))
    return value


def _spreadsheet_products(
    archive: ZipFile,
    template_file: str,
) -> list[EmployeeSpreadsheetProduct]:
    try:
        workbook = ZipFile(BytesIO(archive.read(template_file)))
    except (BadZipFile, KeyError) as error:
        raise EmployeePackageValidationFailure("invalid_template_file") from error

    with workbook:
        try:
            return _spreadsheet_products_from_workbook(workbook)
        except (ElementTree.ParseError, IndexError, KeyError) as error:
            raise EmployeePackageValidationFailure("invalid_template_file") from error


def _spreadsheet_products_from_workbook(
    workbook: ZipFile,
) -> list[EmployeeSpreadsheetProduct]:
    shared_strings = _shared_strings(workbook)
    for name in workbook.namelist():
        if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
            continue
        root = ElementTree.fromstring(workbook.read(name))
        columns: dict[str, str] = {}
        products: list[EmployeeSpreadsheetProduct] = []
        for row in root.iter(f"{SPREADSHEET_NAMESPACE}row"):
            cells = list(row.findall(f"{SPREADSHEET_NAMESPACE}c"))
            if not columns:
                for cell in cells:
                    header = _cell_text(cell, shared_strings).strip()
                    if field_name := PRODUCT_COLUMN_HEADERS.get(header):
                        columns[_cell_column(cell.get("r", ""))] = field_name
                continue
            values: dict[str, str] = {}
            for cell in cells:
                if field_name := columns.get(_cell_column(cell.get("r", ""))):
                    values[field_name] = _cell_text(cell, shared_strings).strip()
            if main_image_reference := values.get("main_image_reference", ""):
                products.append(
                    EmployeeSpreadsheetProduct(
                        main_image_reference=main_image_reference,
                        product_name=values.get("product_name", ""),
                        material_label=values.get("material_label", ""),
                        product_form=values.get("product_form", ""),
                        visual_observations=_spreadsheet_lines(
                            values.get("visual_observations", "")
                        ),
                        selling_points=_spreadsheet_lines(values.get("selling_points", "")),
                        suggested_price=values.get("suggested_price", ""),
                    )
                )
        if "main_image_reference" in columns.values():
            return products
    raise EmployeePackageValidationFailure("main_image_column_missing")


def _spreadsheet_lines(value: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in value.splitlines() if line.strip())


def _is_archive_metadata(name: str) -> bool:
    path = PurePosixPath(name)
    return "__MACOSX" in path.parts or path.name == ".DS_Store" or path.name.startswith("._")


def is_employee_package_workbook(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        path.suffix.lower() == ".xlsx"
        and not path.name.startswith("~$")
        and not _is_archive_metadata(name)
    )


def is_employee_package_image(name: str) -> bool:
    path = PurePosixPath(name)
    return path.suffix.lower() in IMAGE_SUFFIXES and not _is_archive_metadata(name)


def _logical_image_filename(filename: str) -> str:
    path = PurePosixPath(filename)
    stem = re.sub(r"(?:[_ -](?:副本|copy)(?:\s*\d+)?)$", "", path.stem, flags=re.IGNORECASE)
    return f"{stem}{path.suffix}"


def _select_archive_image_files(image_files: list[str]) -> list[str]:
    selected_by_logical_name: dict[str, str] = {}
    for archive_name in image_files:
        filename = PurePosixPath(archive_name).name
        logical_name = _logical_image_filename(filename)
        existing = selected_by_logical_name.get(logical_name)
        if existing is None:
            selected_by_logical_name[logical_name] = archive_name
            continue
        existing_filename = PurePosixPath(existing).name
        if existing_filename != logical_name and filename == logical_name:
            selected_by_logical_name[logical_name] = archive_name
    return list(selected_by_logical_name.values())


def _group_images_without_spreadsheet(
    uploaded_names: list[str],
) -> tuple[EmployeeProductImageGroup, ...]:
    grouped: dict[str, list[tuple[int, str]]] = {}
    for filename in uploaded_names:
        logical_stem = PurePosixPath(_logical_image_filename(filename)).stem
        numbered = re.fullmatch(r"(.+?)-(\d+)[、，,。．]*", logical_stem)
        product_code = numbered.group(1) if numbered else logical_stem
        position = int(numbered.group(2)) if numbered else 1
        grouped.setdefault(product_code, []).append((position, filename))

    products: list[EmployeeProductImageGroup] = []
    for sequence, (product_code, images) in enumerate(grouped.items(), 1):
        ordered = sorted(images, key=lambda item: (item[0], item[1]))
        products.append(
            EmployeeProductImageGroup(
                product_code=product_code,
                sequence=sequence,
                images=tuple(
                    EmployeeProductImage(
                        filename=filename,
                        role="main" if index == 0 else "detail",
                    )
                    for index, (_, filename) in enumerate(ordered)
                ),
            )
        )
    return tuple(products)


def validate_employee_package(content: bytes) -> EmployeePackageValidationResult:
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = [entry for entry in archive.infolist() if not entry.is_dir()]
    except BadZipFile as error:
        raise EmployeePackageValidationFailure("invalid_zip") from error

    for entry in entries:
        if S_ISLNK(entry.external_attr >> 16):
            raise EmployeePackageValidationFailure("unsafe_archive_link")
        name = entry.filename
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise EmployeePackageValidationFailure("unsafe_archive_path")

    names = [entry.filename for entry in entries]

    template_files = [name for name in names if is_employee_package_workbook(name)]
    video_count = sum(PurePosixPath(name).suffix.lower() in VIDEO_SUFFIXES for name in names)
    image_files = _select_archive_image_files(
        [name for name in names if is_employee_package_image(name)]
    )
    if not image_files:
        raise EmployeePackageValidationFailure("missing_product_images")

    def image_only_result(warning: str) -> EmployeePackageValidationResult:
        return EmployeePackageValidationResult(
            template_file="",
            image_count=len(image_files),
            products=_group_images_without_spreadsheet(
                [PurePosixPath(name).name for name in image_files]
            ),
            workbook_count=len(template_files),
            video_count=video_count,
            spreadsheet_warning_zh=warning,
        )

    if not template_files:
        return image_only_result("未找到可用 Excel，图片已导入，商品资料待补充。")

    uploaded_names = [PurePosixPath(name).name for name in image_files]
    parsed_workbooks: list[tuple[str, list[EmployeeSpreadsheetProduct]]] = []
    with ZipFile(BytesIO(content)) as archive:
        for template_file in template_files:
            try:
                spreadsheet_products = _spreadsheet_products(archive, template_file)
            except EmployeePackageValidationFailure:
                continue
            parsed_workbooks.append((template_file, spreadsheet_products))

    if not parsed_workbooks:
        return image_only_result("Excel 未能读取，图片已导入，商品资料待补充。")

    template_file, spreadsheet_products = parsed_workbooks[0]
    if len(parsed_workbooks) > 1 and any(
        candidate[1] != spreadsheet_products for candidate in parsed_workbooks[1:]
    ):
        matching_workbooks: list[
            tuple[
                str,
                tuple[EmployeeProductImageGroup, ...],
                tuple[dict[str, str], ...],
            ]
        ] = []
        for candidate_name, candidate_products in parsed_workbooks:
            try:
                candidate_groups, candidate_warnings = _group_product_images(
                    candidate_products, uploaded_names
                )
            except EmployeePackageValidationFailure:
                continue
            matching_workbooks.append((candidate_name, candidate_groups, candidate_warnings))
        if len(matching_workbooks) == 1:
            template_file, products, filename_warnings = matching_workbooks[0]
            return EmployeePackageValidationResult(
                template_file=template_file,
                image_count=len(image_files),
                products=products,
                workbook_count=len(template_files),
                video_count=video_count,
                filename_warnings=filename_warnings,
            )
        return image_only_result("Excel 与图片编码未对上，图片已导入，商品资料待补充。")

    try:
        products, filename_warnings = _group_product_images(spreadsheet_products, uploaded_names)
    except EmployeePackageValidationFailure:
        return image_only_result("Excel 与图片编码未对上，图片已导入，商品资料待补充。")

    return EmployeePackageValidationResult(
        template_file=template_file,
        image_count=len(image_files),
        products=products,
        workbook_count=len(template_files),
        video_count=video_count,
        filename_warnings=filename_warnings,
    )


def _group_product_images(
    spreadsheet_products: list[EmployeeSpreadsheetProduct],
    uploaded_names: list[str],
) -> tuple[tuple[EmployeeProductImageGroup, ...], tuple[dict[str, str], ...]]:
    if not spreadsheet_products:
        return (
            tuple(
                EmployeeProductImageGroup(
                    product_code=PurePosixPath(filename).stem,
                    sequence=sequence,
                    images=(EmployeeProductImage(filename=filename, role="main"),),
                )
                for sequence, filename in enumerate(uploaded_names, 1)
            ),
            (),
        )
    product_codes = [
        PurePosixPath(product.main_image_reference).stem for product in spreadsheet_products
    ]
    if len(set(product_codes)) != len(product_codes):
        raise EmployeePackageValidationFailure("duplicate_product_code")

    uploaded_by_logical_name = {
        _logical_image_filename(filename): filename for filename in uploaded_names
    }
    uploaded_by_stem = {
        PurePosixPath(logical_name).stem: filename
        for logical_name, filename in uploaded_by_logical_name.items()
    }
    assigned: set[str] = set()
    groups: list[EmployeeProductImageGroup] = []
    filename_warnings: list[dict[str, str]] = []
    for sequence, (spreadsheet_product, product_code) in enumerate(
        zip(spreadsheet_products, product_codes, strict=True),
        1,
    ):
        reference = spreadsheet_product.main_image_reference
        reference_path = PurePosixPath(reference)
        main_filename = (
            uploaded_by_logical_name.get(reference)
            if reference_path.suffix.lower() in IMAGE_SUFFIXES
            else uploaded_by_stem.get(product_code)
        )
        if main_filename not in uploaded_names:
            raise EmployeePackageValidationFailure(
                "image_file_missing",
                missing_main_image_files=[reference],
            )
        assigned.add(main_filename)
        numbered_by_position: dict[int, str] = {}
        prefix = f"{product_code}-"
        for filename in uploaded_names:
            stem = PurePosixPath(_logical_image_filename(filename)).stem
            suffix = stem[len(prefix) :] if stem.startswith(prefix) else ""
            if filename in assigned:
                continue
            position: int | None = None
            interpreted_as: str | None = None
            if suffix.isdigit():
                position = int(suffix)
            elif match := re.fullmatch(r"(\d+)(?:[^0-9A-Za-z_-]{1,8})", suffix):
                position = int(match.group(1))
                interpreted_as = f"{product_code}-{match.group(1)}{PurePosixPath(filename).suffix}"
            if position is None:
                continue
            if existing := numbered_by_position.get(position):
                raise EmployeePackageValidationFailure(
                    "ambiguous_product_images",
                    conflicting_image_files=sorted([existing, filename]),
                )
            numbered_by_position[position] = filename
            if interpreted_as is not None:
                filename_warnings.append(
                    {
                        "original_filename": filename,
                        "interpreted_as": interpreted_as,
                    }
                )
        numbered = sorted(numbered_by_position.items(), key=lambda value: (value[0], value[1]))
        images = [EmployeeProductImage(filename=main_filename, role="main")]
        images.extend(
            EmployeeProductImage(filename=filename, role="detail") for _, filename in numbered
        )
        assigned.update(filename for _, filename in numbered)
        groups.append(
            EmployeeProductImageGroup(
                product_code=product_code,
                sequence=sequence,
                images=tuple(images),
                product_name=spreadsheet_product.product_name,
                material_label=spreadsheet_product.material_label,
                product_form=spreadsheet_product.product_form,
                visual_observations=spreadsheet_product.visual_observations,
                selling_points=spreadsheet_product.selling_points,
                suggested_price=spreadsheet_product.suggested_price,
            )
        )

    unmatched = sorted(set(uploaded_names) - assigned)
    if unmatched:
        raise EmployeePackageValidationFailure(
            "unmatched_product_images",
            unmatched_image_files=unmatched,
        )
    return tuple(groups), tuple(filename_warnings)

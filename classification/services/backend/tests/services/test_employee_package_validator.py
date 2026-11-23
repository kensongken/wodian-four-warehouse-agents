from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest


def package(entries: dict[str, bytes]) -> bytes:
    archive = BytesIO()
    with ZipFile(archive, "w") as zip_file:
        for name, content in entries.items():
            zip_file.writestr(name, content)
    return archive.getvalue()


def workbook_with_main_images(*filenames: str) -> bytes:
    workbook = BytesIO()
    strings = ["主图文件名（必填）", *filenames]
    shared_strings = "".join(f"<si><t>{value}</t></si>" for value in strings)
    rows = [
        '<row r="1"><c r="A1" t="s"><v>0</v></c></row>',
        *(
            f'<row r="{index}"><c r="A{index}" t="s"><v>{index - 1}</v></c></row>'
            for index in range(2, len(strings) + 1)
        ),
    ]
    with ZipFile(workbook, "w") as zip_file:
        zip_file.writestr(
            "xl/sharedStrings.xml",
            f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{shared_strings}</sst>',
        )
        zip_file.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            + "".join(rows)
            + "</sheetData></worksheet>",
        )
    return workbook.getvalue()


def workbook_with_product_details() -> bytes:
    values = [
        "主图文件名（必填）",
        "商品名称（选填）",
        "材质或品类（选填）",
        "商品形态（选填）",
        "可见特点（选填）",
        "销售要点（选填）",
        "建议价格区间（选填）",
        "BCNHDIY913",
        "柿子红圆珠蝴蝶耳饰",
        "南红、S925银镀金",
        "耳饰",
        "圆珠\n蝴蝶造型",
        "日常佩戴\n礼赠",
        "241",
    ]
    shared_strings = "".join(f"<si><t>{value}</t></si>" for value in values)
    columns = "ABCDEFG"
    header = "".join(
        f'<c r="{column}1" t="s"><v>{index}</v></c>' for index, column in enumerate(columns)
    )
    product = "".join(
        f'<c r="{column}2" t="s"><v>{index + 7}</v></c>' for index, column in enumerate(columns)
    )
    workbook = BytesIO()
    with ZipFile(workbook, "w") as zip_file:
        zip_file.writestr(
            "xl/sharedStrings.xml",
            f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{shared_strings}</sst>',
        )
        zip_file.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData><row r="1">{header}</row><row r="2">{product}</row></sheetData>'
            "</worksheet>",
        )
    return workbook.getvalue()


def workbook_with_missing_shared_strings() -> bytes:
    workbook = BytesIO()
    with ZipFile(workbook, "w") as zip_file:
        zip_file.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>'
            "</worksheet>",
        )
    return workbook.getvalue()


def test_employee_package_accepts_template_and_product_images() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "员工商品资料包/商品资料批量上传模板-简版.xlsx": workbook_with_main_images(
                    "示例-001_01.jpg"
                ),
                "员工商品资料包/商品图片/示例-001_01.jpg": b"synthetic image",
            }
        )
    )

    assert result.template_file == "员工商品资料包/商品资料批量上传模板-简版.xlsx"
    assert result.image_count == 1
    assert result.next_step_zh == "资料包结构正确，可以进入图片审核。"


def test_employee_package_reports_workbook_image_and_ignored_video_counts() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "商品资料.xlsx": workbook_with_main_images("BCNHDIY913"),
                "商品图片/BCNHDIY913.jpg": b"main",
                "拍摄视频/BCNHDIY913.mov": b"ignored video",
            }
        )
    )

    assert result.workbook_count == 1
    assert result.image_count == 1
    assert result.video_count == 1


def test_employee_package_finds_workbook_and_images_anywhere_and_ignores_other_files() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "资料/商品清单.xlsx": workbook_with_main_images("BCNHDIY913"),
                "BCNHDIY913.jpg": b"main",
                "拍摄原图/BCNHDIY913-2.jpg": b"detail",
                "说明.pdf": b"ignored",
                ".DS_Store": b"ignored",
                "__MACOSX/._BCNHDIY913.jpg": b"ignored",
            }
        )
    )

    assert result.template_file == "资料/商品清单.xlsx"
    assert result.image_count == 2
    assert result.product_count == 1
    assert [image.filename for image in result.products[0].images] == [
        "BCNHDIY913.jpg",
        "BCNHDIY913-2.jpg",
    ]


def test_employee_package_ignores_macos_and_excel_temporary_workbooks() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "商品清单.xlsx": workbook_with_main_images("BCNHDIY913"),
                "~$商品清单.xlsx": b"temporary workbook",
                "__MACOSX/._商品清单.xlsx": b"macOS metadata",
                "图片/BCNHDIY913.jpg": b"main",
            }
        )
    )

    assert result.template_file == "商品清单.xlsx"
    assert result.image_count == 1


def test_employee_package_selects_the_workbook_that_matches_the_images() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "资料/商品清单.xlsx": workbook_with_main_images("BCNHDIY913"),
                "资料/其他说明.xlsx": workbook_with_main_images("UNRELATED"),
                "图片/BCNHDIY913.jpg": b"main",
                "图片/BCNHDIY913-2.jpg": b"detail",
            }
        )
    )

    assert result.template_file == "资料/商品清单.xlsx"
    assert result.product_count == 1
    assert result.image_count == 2


def test_employee_package_accepts_duplicate_copies_of_the_same_workbook() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    workbook = workbook_with_main_images("BCNHDIY913")
    result = validate_employee_package(
        package(
            {
                "商品清单.xlsx": workbook,
                "备份/商品清单-副本.xlsx": workbook,
                "BCNHDIY913.jpg": b"main",
            }
        )
    )

    assert result.template_file == "商品清单.xlsx"
    assert result.product_count == 1


def test_duplicate_workbooks_still_import_images_when_the_names_do_not_match() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    workbook = workbook_with_main_images("BCNHDIY913")
    result = validate_employee_package(
        package(
            {
                "商品清单.xlsx": workbook,
                "备份/商品清单-副本.xlsx": workbook,
                "图片/OTHER.jpg": b"unmatched",
            }
        )
    )

    assert result.product_count == 1
    assert result.products[0].product_code == "OTHER"
    assert result.spreadsheet_warning_zh


def test_employee_package_matches_images_whose_filenames_have_copy_suffixes() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "商品清单.xlsx": workbook_with_main_images("BCNHDIY913"),
                "图片/BCNHDIY913_副本.jpg": b"main copy",
                "图片/BCNHDIY913-2_副本.jpg": b"detail copy",
            }
        )
    )

    assert result.product_count == 1
    assert result.image_count == 2
    assert [image.filename for image in result.products[0].images] == [
        "BCNHDIY913_副本.jpg",
        "BCNHDIY913-2_副本.jpg",
    ]


def test_employee_package_ignores_named_copies_when_original_images_exist() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "商品清单.xlsx": workbook_with_main_images("BCNHDIY913"),
                "图片/BCNHDIY913.jpg": b"main",
                "图片/BCNHDIY913_副本.jpg": b"main copy",
                "图片/BCNHDIY913-2.jpg": b"detail",
                "图片/BCNHDIY913-2_副本.jpg": b"detail copy",
            }
        )
    )

    assert result.image_count == 2
    assert [image.filename for image in result.products[0].images] == [
        "BCNHDIY913.jpg",
        "BCNHDIY913-2.jpg",
    ]


def test_employee_package_rejects_missing_product_image_folder() -> None:
    from product_engine.services.employee_package_validator import (
        EmployeePackageValidationFailure,
        validate_employee_package,
    )

    with pytest.raises(EmployeePackageValidationFailure, match="missing_product_images"):
        validate_employee_package(
            package(
                {
                    "员工商品资料包/商品资料批量上传模板-简版.xlsx": workbook_with_main_images(
                        "示例-001_01.jpg"
                    )
                }
            )
        )


def test_employee_package_imports_images_when_the_workbook_is_malformed() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "员工商品资料包/商品资料批量上传模板-简版.xlsx": (
                    workbook_with_missing_shared_strings()
                ),
                "员工商品资料包/商品图片/BCNHDIY913.jpg": b"main",
                "员工商品资料包/商品图片/BCNHDIY913-2.jpg": b"detail",
            }
        )
    )

    assert result.product_count == 1
    assert result.image_count == 2
    assert result.spreadsheet_warning_zh == "Excel 未能读取，图片已导入，商品资料待补充。"


def test_employee_package_imports_images_when_the_workbook_names_do_not_match() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "员工商品资料包/商品资料批量上传模板-简版.xlsx": workbook_with_main_images(
                    "示例-001_01.jpg"
                ),
                "员工商品资料包/商品图片/BCNHDIY913.jpg": b"main",
                "员工商品资料包/商品图片/BCNHDIY913-2.jpg": b"detail",
            }
        )
    )

    assert result.product_count == 1
    assert result.products[0].product_code == "BCNHDIY913"
    assert result.spreadsheet_warning_zh == "Excel 与图片编码未对上，图片已导入，商品资料待补充。"


def test_employee_package_groups_numbered_images_without_a_workbook() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "BCNHDIY913.jpg": b"main",
                "BCNHDIY913-2.jpg": b"detail",
                "BCNHDIY914.jpg": b"second product",
            }
        )
    )

    assert result.template_file == ""
    assert result.product_count == 2
    assert result.image_count == 3
    assert [product.product_code for product in result.products] == [
        "BCNHDIY913",
        "BCNHDIY914",
    ]
    assert [image.role for image in result.products[0].images] == ["main", "detail"]
    assert result.spreadsheet_warning_zh == "未找到可用 Excel，图片已导入，商品资料待补充。"


def test_employee_package_rejects_unsafe_archive_path() -> None:
    from product_engine.services.employee_package_validator import (
        EmployeePackageValidationFailure,
        validate_employee_package,
    )

    with pytest.raises(EmployeePackageValidationFailure, match="unsafe_archive_path"):
        validate_employee_package(
            package(
                {
                    "../员工商品资料包/商品资料批量上传模板-简版.xlsx": workbook_with_main_images(
                        "示例-001_01.jpg"
                    ),
                    "员工商品资料包/商品图片/示例-001_01.jpg": b"synthetic image",
                }
            )
        )


def test_employee_package_rejects_symbolic_link() -> None:
    from product_engine.services.employee_package_validator import (
        EmployeePackageValidationFailure,
        validate_employee_package,
    )

    archive = BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr(
            "员工商品资料包/商品资料批量上传模板-简版.xlsx",
            workbook_with_main_images("示例-001_01.jpg"),
        )
        link = ZipInfo("员工商品资料包/商品图片/示例-001_01.jpg")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        zip_file.writestr(link, "../../outside.jpg", compress_type=ZIP_DEFLATED)

    with pytest.raises(EmployeePackageValidationFailure, match="unsafe_archive_link"):
        validate_employee_package(archive.getvalue())


def test_employee_package_groups_one_product_code_with_main_and_numbered_images() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "员工商品资料包/商品资料批量上传模板-简版.xlsx": (
                    workbook_with_main_images("BCNHDIY913")
                ),
                "员工商品资料包/商品图片/BCNHDIY913.jpg": b"main",
                "员工商品资料包/商品图片/BCNHDIY913-2.jpg": b"detail",
            }
        )
    )

    assert result.product_count == 1
    assert result.image_count == 2
    assert result.products[0].product_code == "BCNHDIY913"
    assert [image.filename for image in result.products[0].images] == [
        "BCNHDIY913.jpg",
        "BCNHDIY913-2.jpg",
    ]
    assert [image.role for image in result.products[0].images] == ["main", "detail"]


def test_employee_package_reads_product_details_from_the_matching_workbook_row() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "员工商品资料包/商品资料批量上传模板-简版.xlsx": (workbook_with_product_details()),
                "员工商品资料包/商品图片/BCNHDIY913.jpg": b"main",
                "员工商品资料包/商品图片/BCNHDIY913-2.jpg": b"detail",
            }
        )
    )

    product = result.products[0]
    assert product.product_name == "柿子红圆珠蝴蝶耳饰"
    assert product.material_label == "南红、S925银镀金"
    assert product.product_form == "耳饰"
    assert product.visual_observations == ("圆珠", "蝴蝶造型")
    assert product.selling_points == ("日常佩戴", "礼赠")
    assert product.suggested_price == "241"


def test_employee_package_safely_normalizes_a_unique_punctuation_suffix() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "员工商品资料包/商品资料批量上传模板-简版.xlsx": (
                    workbook_with_main_images("BCNHDIY989")
                ),
                "员工商品资料包/商品图片/BCNHDIY989.jpg": b"main",
                "员工商品资料包/商品图片/BCNHDIY989-2、.jpg": b"typo",
            }
        )
    )

    assert [image.filename for image in result.products[0].images] == [
        "BCNHDIY989.jpg",
        "BCNHDIY989-2、.jpg",
    ]
    assert result.filename_warnings == (
        {
            "original_filename": "BCNHDIY989-2、.jpg",
            "interpreted_as": "BCNHDIY989-2.jpg",
        },
    )


def test_employee_package_safely_normalizes_a_unique_mojibake_suffix() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "商品资料.xlsx": workbook_with_main_images("BCNHDIY989"),
                "商品图片/BCNHDIY989.jpg": b"main",
                "商品图片/BCNHDIY989-2πÇü.jpg": b"detail",
            }
        )
    )

    assert [image.filename for image in result.products[0].images] == [
        "BCNHDIY989.jpg",
        "BCNHDIY989-2πÇü.jpg",
    ]
    assert result.filename_warnings == (
        {
            "original_filename": "BCNHDIY989-2πÇü.jpg",
            "interpreted_as": "BCNHDIY989-2.jpg",
        },
    )


def test_employee_package_keeps_both_images_when_numbered_slots_conflict() -> None:
    from product_engine.services.employee_package_validator import validate_employee_package

    result = validate_employee_package(
        package(
            {
                "员工商品资料包/商品资料批量上传模板-简版.xlsx": (
                    workbook_with_main_images("BCNHDIY989")
                ),
                "员工商品资料包/商品图片/BCNHDIY989.jpg": b"main",
                "员工商品资料包/商品图片/BCNHDIY989-2.jpg": b"detail",
                "员工商品资料包/商品图片/BCNHDIY989-2、.jpg": b"conflict",
            }
        )
    )

    assert result.product_count == 1
    assert result.image_count == 3
    assert len(result.products[0].images) == 3
    assert result.spreadsheet_warning_zh

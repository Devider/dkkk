import json
import os
from pathlib import Path

import aiofiles
import ujson


def filepath_from_env_validator(filepath: str):
    """Проверяет, существует ли путь до файла.

    Args:
        filepath (str): путь к файлу

    Raises:
        ValueError: если указанный путь не является строкой
        FileNotFoundError: если файла не существует
        IsADirectoryError: если файл является директорией
    """
    if not isinstance(filepath, str):
        raise ValueError(f"Path to file must be a 'string' value. Got: '{type(filepath)}'")
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File does not exist: '{filepath}'")
    if not os.path.isfile(filepath):
        raise IsADirectoryError(f"Expected a path to file, but got: {filepath}")


def dirpath_from_env_validator(dirpath: str):
    """Проверяет, существует ли путь до директории.

    Args:
        dirpath (str): путь к директории

    Raises:
        ValueError: если указанный путь не является строкой
        FileNotFoundError: если дирекории не существует
        NotADirectoryError: если путь не является директорией
    """
    if not isinstance(dirpath, str):
        raise ValueError(f"Path to directory must be a 'string' value. Got: '{type(dirpath)}'")
    if not os.path.exists(dirpath):
        raise FileNotFoundError(f"Directory does not exist: '{dirpath}'")
    if not os.path.isdir(dirpath):
        raise NotADirectoryError(f"Path is not a directory: '{dirpath}'")


async def async_get_bytes(file_path: Path) -> bytes:
    async with aiofiles.open(file_path, mode="rb") as fp:
        return await fp.read()


async def async_get_file(file_path: Path) -> str:
    async with aiofiles.open(file_path, mode="r", encoding="utf-8") as file:
        return await file.read()


async def async_get_json(file_path: Path) -> dict:
    data = await async_get_file(file_path)
    return ujson.loads(data)


def get_json(file_path: Path) -> dict:
    with open(file_path, encoding="utf-8") as fp:
        return json.load(fp)


def get_file(file_path: Path) -> str:
    with open(file_path, mode="r", encoding="utf-8") as fp:
        return fp.read()


def get_bytes(file_path: Path) -> bytes:
    with open(file_path, mode="rb") as fp:
        return fp.read()

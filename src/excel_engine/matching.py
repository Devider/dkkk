"""Semantic-layer helpers used by the calculations: copy of aigw_service.api.v1.tools (story S0.7, bodies unchanged)."""

import re

from aigw_service.context import APP_CTX
from excel_engine.russian_stemmer import russian_stem

logger = APP_CTX.get_logger()


def extract_year_from_query(query: str) -> int:
    """Extract year from a query string."""
    import re

    year_match = re.search(r"20\d{2}", query)
    if year_match:
        return int(year_match.group())
    return None


def normalize_text(text: str) -> str:
    """Normalize text for better matching, including Russian stemming.

    Args:
        text: Text to normalize

    Returns:
        str: Normalized text with stemmed Russian words
    """
    # Convert to lowercase
    text = text.lower()

    # Remove special characters and extra spaces
    text = re.sub(r"[^\w\s]", " ", text)

    # Stem each word
    words = text.split()
    stemmed_words = [russian_stem(word) for word in words]

    # Join words back together
    text = " ".join(stemmed_words)

    # Normalize spaces
    text = " ".join(text.split())

    return text


def create_input_mapping(data: list[list]) -> dict:
    """Create a structured mapping of input descriptions to their cells.

    *data* is a 2‑D list returned by :meth:`ExcelWorkbook.get_all_data`.
    """
    try:
        if not data:
            raise ValueError("No data found in inputs sheet")

        # First row contains headers
        headers = data[0]

        # Find the column index for variable names (column F)
        name_col_idx = None
        for i, header in enumerate(headers):
            if header == "Наименование":
                name_col_idx = i
                break

        if name_col_idx is None:
            raise ValueError("Could not find 'Наименование' column")

        # Find year columns
        year_columns = {}
        for i, header in enumerate(headers):
            try:
                year = float(header)
                if 2000 <= year <= 2100:
                    year_columns[i] = int(year)
            except (ValueError, TypeError):
                continue

        if not year_columns:
            raise ValueError("No year columns found in headers")

        # logger.info(f"Found year columns: {year_columns}")  # Commented out

        # Create mapping of row descriptions to their values
        row_mapping = {}
        for row_idx, row in enumerate(data[1:], start=2):  # Start from row 2 (after headers)
            if not row:  # Skip completely empty rows
                # logger.debug(f"Skipping empty row {row_idx}")  # Commented out
                continue

            # Ensure row has enough columns
            if len(row) <= name_col_idx:
                # logger.debug(f"Row {row_idx} too short: {row}")  # Commented out
                continue

            # Get the variable name from column F
            description = str(row[name_col_idx]).strip() if row[name_col_idx] is not None else ""
            if not description:
                # logger.debug(f"Empty description in row {row_idx}")  # Commented out
                continue

            # Store the normalized description and its original form
            normalized_desc = normalize_text(description)

            # Get values for each year column
            values = {}
            for col_idx, year in year_columns.items():
                if col_idx < len(row):
                    value = row[col_idx]
                    if value is not None:
                        try:
                            # Try to convert to float if possible
                            values[year] = float(value)
                        except (ValueError, TypeError):
                            values[year] = value

            row_mapping[normalized_desc] = {"original": description, "row": row_idx, "values": values}

        return {"year_columns": year_columns, "row_mapping": row_mapping}

    except Exception as e:
        logger.opt(exception=True).error("Error in create_input_mapping: {}", str(e))
        raise


def jaccard_similarity(str1: str, str2: str) -> float:
    """Calculate Jaccard similarity between two strings.

    Args:
        str1: First string to compare
        str2: Second string to compare

    Returns:
        float: Jaccard similarity score between 0 and 1
    """
    # Convert strings to sets of words
    set1 = set(str1.lower().split())
    set2 = set(str2.lower().split())

    # Calculate intersection and union
    intersection = len(set1.intersection(set2))
    union = len(set1.union(set2))

    # Return Jaccard similarity
    return intersection / union if union > 0 else 0.0


def find_matching_cell(
    query: str,
    input_mapping: dict,
    default_year: int = None,
    return_best_score: bool = False,
) -> tuple:
    """Find the single most matching cell for a given query using Jaccard similarity. If year is not found, use default_year if provided."""
    # Extract year from query
    target_year = extract_year_from_query(query)
    if not target_year and default_year:
        target_year = default_year
    if not target_year:
        raise ValueError(f"No year found in query: {query} and no default year provided.")

    # Normalize query without the year
    normalized_query = normalize_text(query.replace(str(target_year), "").strip())

    # Find best matching row based only on description
    best_match = None
    best_score = 0

    for norm_desc, row_info in input_mapping["row_mapping"].items():
        # Calculate Jaccard similarity score
        score = jaccard_similarity(normalized_query, norm_desc)

        if score > best_score:
            best_score = score
            best_match = row_info

    if not best_match or best_score < 0.1:  # Very low threshold since we want the best match even if not great
        raise ValueError(f"No match found for query: {query}")

    # Find the column index for the target year
    year_col_idx = None
    for col_idx, year in input_mapping["year_columns"].items():
        if year == target_year:
            year_col_idx = col_idx
            break

    if year_col_idx is None:
        raise ValueError(f"Year {target_year} not found in headers")

    # Convert to Excel column letter (handling columns beyond Z)
    col_letter = ""
    while year_col_idx >= 0:
        col_letter = chr(ord("A") + (year_col_idx % 26)) + col_letter
        year_col_idx = year_col_idx // 26 - 1

    # Return proper xlwings cell reference format
    cell_reference = f"{col_letter}{best_match['row']}"
    if return_best_score:
        return cell_reference, best_match["original"], best_score
    return cell_reference, best_match["original"]


def create_output_mapping(data: list[list]) -> dict:
    """Create a structured mapping of output descriptions to their cells.

    *data* is a 2‑D list returned by :meth:`ExcelWorkbook.get_all_data`.
    """
    try:
        if not data:
            raise ValueError("No data found in outputs sheet")

        logger.info(f"Output data structure: {len(data)} rows")
        if data:
            logger.info(f"First row: {data[0]}")

        # First row contains headers
        headers = data[0]
        logger.info(f"Output headers: {headers}")

        # Find the column index for variable names (column F)
        name_col_idx = None
        for i, header in enumerate(headers):
            if header == "Наименование":
                name_col_idx = i
                break

        if name_col_idx is None:
            raise ValueError("Could not find 'Наименование' column")

        logger.info(f"Found 'Наименование' column at index: {name_col_idx}")

        # Find year columns
        year_columns = {}
        for i, header in enumerate(headers):
            try:
                year = float(header)
                if 2000 <= year <= 2100:
                    year_columns[i] = int(year)
            except (ValueError, TypeError):
                continue

        if not year_columns:
            raise ValueError("No year columns found in headers")

        logger.info(f"Found year columns: {year_columns}")

        # Create mapping of output descriptions to their values
        output_mapping = {}
        for row_idx, row in enumerate(data[1:], start=2):  # Start from row 2 (after headers)
            if not row:  # Skip completely empty rows
                logger.debug(f"Skipping empty row {row_idx}")
                continue

            # Ensure row has enough columns
            if len(row) <= name_col_idx:
                logger.debug(f"Row {row_idx} too short: {row}")
                continue

            # Get the variable name from column F
            description = str(row[name_col_idx]).strip() if row[name_col_idx] is not None else ""
            if not description:
                logger.debug(f"Empty description in row {row_idx}")
                continue

            # Store the normalized description and its original form
            normalized_desc = normalize_text(description)

            # Get values for each year column
            values = {}
            for col_idx, year in year_columns.items():
                if col_idx < len(row):
                    value = row[col_idx]
                    if value is not None:
                        try:
                            # Try to convert to float if possible
                            values[year] = float(value)
                        except (ValueError, TypeError):
                            values[year] = value

            output_mapping[normalized_desc] = {"original": description, "row": row_idx, "values": values}

            if len(output_mapping) <= 5:  # Log first 5 entries for debugging
                logger.info(f"Added mapping for: {description} (normalized: {normalized_desc})")
                logger.info(f"Values: {values}")

        logger.info(f"Created output mapping with {len(output_mapping)} entries")
        return {"year_columns": year_columns, "output_mapping": output_mapping}

    except Exception as e:
        logger.opt(exception=True).error("Error in create_output_mapping: {}", str(e))
        raise


def find_matching_outputs(query: str, output_mapping: dict, return_best_score: bool = False) -> dict:
    """Find the single most matching output for a given query using Jaccard similarity."""
    # Extract year from query if present
    target_year = extract_year_from_query(query)

    # Normalize query
    normalized_query = normalize_text(query)

    # Find best matching output
    best_match = None
    best_score = 0

    for norm_desc, output_info in output_mapping["output_mapping"].items():
        # Calculate Jaccard similarity score
        score = jaccard_similarity(normalized_query, norm_desc)

        if score > best_score:
            best_score = score
            best_match = (output_info["original"], output_info)

    if not best_match or best_score < 0.1:  # Very low threshold since we want the best match even if not great
        return {"_best_score": best_score} if return_best_score else {}

    output_name, output_info = best_match

    # If year is specified, only include that year's value
    if target_year:
        if target_year in output_info["values"]:
            result = {output_name: {"value": output_info["values"][target_year], "year": target_year}}
            if return_best_score:
                result["_best_score"] = best_score
            return result
    else:
        # Include all years if no specific year requested
        result = {output_name: {"values": output_info["values"]}}
        if return_best_score:
            result["_best_score"] = best_score
        return result

    return {"_best_score": best_score} if return_best_score else {}

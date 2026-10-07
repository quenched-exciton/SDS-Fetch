# SDS Fetch

A small desktop program that downloads Safety Data Sheet (SDS) PDFs for a list of chemicals, identified by CAS Registry Number. Each file is saved as `<CAS Number>.pdf` (for example `7758-99-8.pdf`) in a folder you choose. At the end, the program writes a log file in that folder that lists every CAS number as **successful**, **failed** or **invalid**.

![SDS Fetch window](docs/screenshot.png)

*Screenshot from a simulated run with offline test data.*

## Installation

You need Python 3.9 or newer. Use Python 3.10 or newer on a company network (see *Company networks* below).

1. **Install Python** from <https://www.python.org/downloads/>. On Windows, tick **"Add python.exe to PATH"** in the installer and keep the **"tcl/tk and IDLE"** option ticked; the program window is built with Tk.
2. **Download this project**: on GitHub click **Code → Download ZIP** and unzip it, or run `git clone https://github.com/quenched-exciton/SDS-Fetch.git`.
3. **Install the helper packages** (requests, beautifulsoup4, pypdf, truststore, openpyxl). Open a terminal (Windows: *Command Prompt*) in the project folder and run:

   ```
   python -m pip install -r requirements.txt
   ```

   On Windows, use `py` if `python` is not found: `py -m pip install -r requirements.txt`.

## Running the program

From the project folder:

```
python run_sds_fetch.py
```

`python -m sds_fetch` does the same thing.

## Using the window

1. **Fill in the chemical list.** Type or paste one chemical per line:

   ```
   # Nickel bath additives
   7758-99-8
   2530-83-8 @ Gelest          # epoxy silane
   81-07-2    Saccharin
   ```

   * `@ name` asks for that manufacturer's SDS (see *Choosing the manufacturer* below).
   * Text after `#` is a note and is ignored, and so is a chemical name pasted next to the CAS number. You can paste two columns straight from Excel.
   * Several CAS numbers on one line also work when separated by commas, semicolons, tabs or spaces.
   * **Load CSV / Excel file…** reads a `.csv`, `.txt` or `.xlsx` file into the list, where you can check and edit it before running. It uses the column whose header contains "CAS" (otherwise the first column), a column headed "Manufacturer", "Supplier", "Brand" or "Vendor" for the `@` part, and a "Name" or "Chemical" column as the note. See [`examples/example_cas_list.csv`](examples/example_cas_list.csv).
   * **Failed from last run** loads the CAS numbers that got no SDS in the last run (with their `@` manufacturers), so you can retry them after fixing a network problem.
   * **The list is checked as you type.** Lines without a valid CAS number turn light red, repeated CAS numbers light yellow, and notes grey. The line below the box gives the count and the first problem. Old `.xls` files have to be saved as `.xlsx` or CSV first. If Excel has turned a CAS number into a date (for example 75-05-8 into 8 May 1975), the line shows `Excel date 1975-05-08`: retype it, and format the CAS column as *Text* in Excel to stop it happening again.
2. **Choose the manufacturer (optional).** See below.
3. **Choose the download folder.** Tick *Overwrite PDFs that already exist* if you want to replace SDS files you downloaded before. Otherwise the program skips them.
4. **Press Run** (or Ctrl+Enter in the list). If some lines have problems, the program lists them and asks before it continues. The progress panel at the bottom shows what the program is doing for each chemical. **Stop** finishes the current chemical and then stops; the log lists the unprocessed CAS numbers.

When the run is finished, a message shows the totals and offers to open the download folder.

The window remembers the list, the folder, the manufacturer settings and the tick boxes until next time. They are saved in `.sds_fetch_settings.json` in your user folder (Windows: `C:\Users\<you>`); delete that file to start empty.

## Choosing the manufacturer

An SDS downloaded by CAS number may come from any manufacturer. If you need a particular one, for example the supplier you actually buy from, there are two ways to ask for it:

* **For one chemical:** add `@` and the name to its line, e.g. `2530-83-8 @ Gelest`.
* **For the whole list:** type names in the *Preferred* box, most wanted first, e.g. `Sigma-Aldrich, Thermo Fisher`. Lines with their own `@` name try that one first.

Brand names count as their company: Merck, MilliporeSigma, Aldrich, Fluka and Supelco all mean Sigma-Aldrich; Acros, Alfa Aesar and Fisher mean Thermo Fisher; J.T. Baker and Macron mean Avantor / VWR. The full list is at the top of [`sds_fetch/manufacturers.py`](sds_fetch/manufacturers.py), where you can add more. Names that are not in the list are matched as typed, ignoring capitals and punctuation.

How the program picks the SDS:

1. As soon as a website lists an SDS from your first-choice manufacturer, the program downloads and checks it. If it is good, the remaining websites are not searched.
2. Otherwise it asks all websites first, then tries your other requested manufacturers in order, then SDS files whose manufacturer the website does not name.
3. Without **Strict**, if none of those works, it saves another manufacturer's SDS and the log says so (`From requested: no`). With **Strict**, it saves nothing from other manufacturers; the FAILED list then shows which manufacturers the websites did offer, so you can choose one.
4. Every saved SDS is checked for the manufacturer's name in its Section 1. The log and the hazard summary say `yes` (named in the PDF), `website only` (the website said so, but the PDF text does not name it) or `no`.

Limits: the program can only find manufacturers that its five websites carry. ChemicalSafety.com, VWR and ChemBlink list SDS files from many manufacturers; Fisher Scientific only has Thermo Fisher brands and Fluorochem only its own. A run with a requested manufacturer is slower when that manufacturer is not found early, because every website is searched before another manufacturer is accepted.

## What you get

* `<CAS>.pdf` for every chemical with an SDS, e.g. `7664-93-9.pdf`.
* `SDS_download_log_<date>_<time>.txt` with these sections:
  * **SUCCESSFUL:** CAS number, file name, chemical name from PubChem, the website it came from, the supplier, and whether the CAS number was found in the PDF text.
  * **FAILED:** valid CAS numbers without an SDS, plus the result from each website (e.g. `HTTP 403`, `no SDS found`). This tells you whether a site blocked the request or simply doesn't have the chemical.
  * **INVALID:** entries that are not CAS numbers or that fail the CAS check digit (usually a typo).
  * **OUTDATED SDS:** downloaded or existing SDS files older than 3 years (see below).
  * At the top, a `Python:` line with the version and location of the Python that ran the program, and a `Certificates:` line that says whether the Windows certificate store is used and, if not, why not and how to fix it.
* `SDS_hazard_summary_<date>_<time>.csv`, which opens in Excel, with one row per CAS number in the order you entered them:

  | Column | Content |
  |---|---|
  | Status | downloaded, already in folder, or no SDS found |
  | Requested manufacturer, From requested manufacturer | what you asked for with `@` or *Preferred*, and whether the SDS is from it (`yes`, `website only`, `no`) |
  | Signal word | Danger, Warning, none (not classified) or not found |
  | H-codes | GHS hazard statement codes, e.g. `H302, H319, H410` |
  | Hazard statements | each code with its standard sentence, e.g. `H302 Harmful if swallowed` |
  | Hazards read from | `Section 2`, or `whole SDS` when the Section 2 heading was not recognised |
  | SDS date, Date type, Age | the revision date (or issue date if the SDS has no revision date) and its age in years |
  | Outdated | `YES` when the SDS is more than 3 years old |

## Hazard summary: how it works and its limits

The program reads the text of each saved SDS and looks only at **Section 2 (Hazards identification)**, which holds the classification of the product itself. Other sections list hazards that do not apply to the product as a whole: Section 3 and Section 16 give those of individual ingredients, and Section 11 quotes toxicology data. It collects H-codes printed in the text (EU-style SDSs) and also recognises the standard English sentences of the GHS hazard statements, because US SDSs often print only the sentence. SDS files that already existed in the folder are read too, so the summary always covers the whole list.

The summary is an overview for planning, not a substitute for reading Section 2:

* Hazard pictograms are images and cannot be read. The H-codes carry the same information.
* PDF text extraction is sometimes imperfect, and only English-language SDSs are understood. A scanned SDS gives `PDF text unreadable`.
* Dates written as `05/12/2023` are read as US month/day/year unless the first number is above 12.
* **Outdated** means older than 3 years. This is a common internal review interval, not a legal limit: OSHA HazCom sets no expiry date, and REACH requires suppliers to update an SDS when new hazard information becomes available. An old SDS can still be the current one for an unchanged product. Change `SDS_MAX_AGE_YEARS` at the top of [`sds_fetch/sds_info.py`](sds_fetch/sds_info.py) to use a different limit.

## How it works

1. **Validation (offline).** Every CAS number includes a check digit. The program removes the check digit, reads the remaining digits from right to left, multiplies them by 1, 2, 3, … and adds the products. The last digit of that sum must equal the check digit. Water, 7732-18-5: 8×1 + 1×2 + 2×3 + 3×4 + 7×5 + 7×6 = 105 → check digit 5 ✔. Entries that fail go to the INVALID list and are never searched.
2. **Chemical identity.** [PubChem PUG-REST](https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest) returns the name for each CAS number, which appears in the progress panel and in the log. Polymers, mixtures and some industrial products are often missing from PubChem. For those, the SDS search still runs.
3. **SDS search.** The program tries these websites in order and stops at the first good result:

   | Order | Website | How it searches |
   |---|---|---|
   | 1 | Fisher Scientific | SDS search page |
   | 2 | ChemicalSafety.com | free SDS database with sheets from many manufacturers (Sigma-Aldrich, Fisher, VWR, …) |
   | 3 | VWR / Avantor | SDS search page |
   | 4 | Fluorochem | product search service |
   | 5 | ChemBlink | one SDS page per CAS number |

4. **Checks before saving.** Each downloaded file must really be a PDF, and the program reads the first pages to confirm that the CAS number is printed in it. If the PDF names a different chemical, the program rejects it and tries the next link. If the PDF has no readable text (for example a scanned image), the program keeps it only when the website itself listed it under that exact CAS number. The log then marks it `NO - check manually`.

## Important limitations

* **Supplier websites change and some block automated downloads.** A website that is redesigned or starts refusing requests turns into `HTTP 403`, `no SDS found` or `search failed` messages in the log. The program then moves on to the next source. The website-reading code has automated tests against sample pages only, so a live website can behave differently. See *When a source stops working* below.
* **Use the SDS for the product you actually bought.** An SDS downloaded by CAS number may come from a different supplier than your product. Grade, composition, hazard classification and revision date can differ. OSHA HazCom (29 CFR 1910.1200) and REACH require the SDS that ships with your purchased product, so treat these downloads as a reference.
* **Check the supplier's terms of use.** Some websites restrict automated access. The program waits one second between chemicals and sends one request at a time, so keep your lists to a reasonable size.

## Company networks

If the log says **NETWORK PROBLEM** or every website reports `security certificate refused`, your network intercepts HTTPS traffic. Many companies run a security proxy (Zscaler, Netskope, Palo Alto and similar) that decrypts HTTPS traffic for inspection and re-signs it with the company's own certificate. IT installs that certificate in Windows, so web browsers accept it. Python normally uses its own built-in certificate list instead, so it refuses every connection with `CERTIFICATE_VERIFY_FAILED ... self signed certificate in certificate chain`.

The program fixes this with the [truststore](https://pypi.org/project/truststore/) package, which makes Python trust the same certificates as Windows. It is in `requirements.txt`, so run `python -m pip install -r requirements.txt` again after updating. The `Certificates:` line at the top of the log shows whether it is active, and if not, why. A common cause is having more than one Python installed: packages installed with `py -m pip` are invisible to `python`, and the reverse. The `Python:` line shows which Python ran the program, and the `Certificates:` line then gives the exact install command for that Python. truststore needs Python 3.10 or newer. On Python 3.9, either upgrade Python or ask IT for the company root certificate as a `.pem` file and set the environment variable `REQUESTS_CA_BUNDLE` to its path.

If `pip` itself fails with the same certificate error, first run `python -m pip install --upgrade pip`. pip 24.2 and newer already use the Windows certificate store.

Never switch certificate checking off to get around this: the program would then accept files from any server that pretends to be a supplier website.

## When a source stops working

Each website has its own function in [`sds_fetch/sources.py`](sds_fetch/sources.py). The list at the bottom of that file sets the search order:

```python
SDS_SOURCES = [
    ("Fisher Scientific", find_on_fisher),
    ("ChemicalSafety.com", find_on_chemicalsafety),
    ...
]
```

Move a line up or down to change the order, or put `#` in front of a line to switch that source off. Timeouts and the number of links tried per website are set at the top of the same file.

## Project layout

```
run_sds_fetch.py             start the program
sds_fetch/
    cas.py                   read the list / CSV / Excel file, check CAS numbers
    manufacturers.py         manufacturer names and their brands
    settings.py              remember the window's settings
    sources.py               PubChem name lookup + one search function per SDS website
    downloader.py            download, verify, save <CAS>.pdf, write the log and the hazard summary
    sds_info.py              read signal word, H-codes and SDS date from the PDF text
    gui.py                   the window (Tkinter)
examples/example_cas_list.csv
tests/                       automated tests (no internet needed)
```

## Running the tests

```
python -m pip install -r requirements-dev.txt
python -m pytest
```

The tests use fake websites and generated PDFs, so they run offline in about a second. GitHub runs them automatically on every push (see `.github/workflows/tests.yml`).

## Acknowledgements

The open-source project [find_sds](https://github.com/khoivan88/find_sds) by Khoi Van showed which supplier websites offer SDS search by CAS number. This program is a separate implementation.

## License

MIT. See [LICENSE](LICENSE).

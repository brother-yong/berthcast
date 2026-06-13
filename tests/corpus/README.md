# Broken-file corpus

Small, deliberately messy upload files. Each one stands for a *category* of mess
a real distributor (or a free-tier stranger) could upload. `test_data_safety_net.py`
runs every file through the real ingestion and the real `data_quality.assess_upload`
gate and asserts the outcome it must produce: a clean run (OK), a specific WARN, or
a BLOCK.

We can't test the infinite real world. We can prove every kind of mess we know of
ends in a safe state, and keep it that way as the code changes.

| File(s) | Stands for | Required outcome |
|---|---|---|
| clean_inventory.csv + clean_sales.csv | a good upload | OK (no findings) |
| wrong_document.csv | someone uploaded the wrong file (a payslip) | BLOCK |
| empty_inventory.csv | empty / header-only file | BLOCK |
| stock_is_text.csv | the stock column holds words, not numbers | BLOCK |
| euro_decimals_inventory.csv | "1.200,50" Indonesian/European numbers | WARN number_format |
| codes_inventory.csv + codes_sales.csv | sales use codes, inventory uses names | WARN low_name_overlap |
| unit_inventory.csv + unit_sales.csv | inventory in CTN, sales in KG | WARN unit_mismatch |
| suspect_stock_inventory.csv | stock mapped to an "on order" column | WARN stock_column_suspect |
| (generated in-test) | 4000+ row inventory | WARN large_file |

import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";


function parseArgs(argv) {
  if (argv.length === 0 || argv[0].startsWith("--")) {
    throw new Error("用法: ignore_report.mjs <date_dir> --node-modules <path> --preview-dir <path>");
  }

  const result = {
    dateDir: path.resolve(argv[0]),
    nodeModules: null,
    previewDir: null,
  };
  for (let index = 1; index < argv.length; index += 1) {
    const option = argv[index];
    const value = argv[index + 1];
    if (option === "--node-modules" && value) {
      result.nodeModules = path.resolve(value);
      index += 1;
    } else if (option === "--preview-dir" && value) {
      result.previewDir = path.resolve(value);
      index += 1;
    } else {
      throw new Error(`未知或缺少参数值: ${option}`);
    }
  }
  if (!result.nodeModules) {
    throw new Error("缺少 --node-modules；必须使用 load_workspace_dependencies 提供的运行时依赖。");
  }
  if (!result.previewDir) {
    throw new Error("缺少 --preview-dir；预览文件必须写入临时目录。");
  }
  return result;
}


async function importArtifactTool(nodeModulesPath) {
  const loaderPath = path.join(path.dirname(nodeModulesPath), "ignore-report-loader.cjs");
  const runtimeRequire = createRequire(loaderPath);
  const artifactEntry = runtimeRequire.resolve("@oai/artifact-tool");
  return import(pathToFileURL(artifactEntry).href);
}


async function isFile(filePath) {
  try {
    return (await fs.stat(filePath)).isFile();
  } catch (error) {
    if (error && error.code === "ENOENT") {
      return false;
    }
    throw error;
  }
}


function safeCellText(value) {
  const normalized = value.replace(/\r\n?/g, "\n");
  return /^[=+\-@]/.test(normalized) ? `'${normalized}` : normalized;
}


async function collectIgnoreRows(dateDir) {
  const ignoreDir = path.join(dateDir, "ignore");
  let entries;
  try {
    entries = await fs.readdir(ignoreDir, { withFileTypes: true });
  } catch (error) {
    if (error && error.code === "ENOENT") {
      return { rows: [], errors: [] };
    }
    throw error;
  }

  const caseDirs = entries
    .filter((entry) => entry.isDirectory())
    .sort((left, right) => left.name.localeCompare(right.name, "en", { sensitivity: "base" }));
  if (caseDirs.length === 0) {
    return { rows: [], errors: [] };
  }

  const rows = [];
  const errors = [];
  const seenNames = new Map();
  for (const caseEntry of caseDirs) {
    const caseDir = path.join(ignoreDir, caseEntry.name);
    const errorJsonPath = path.join(caseDir, "error.json");
    if (!(await isFile(errorJsonPath))) {
      errors.push(`${caseDir}: 缺少 error.json`);
      continue;
    }

    let payload;
    try {
      payload = JSON.parse(await fs.readFile(errorJsonPath, "utf8"));
    } catch (error) {
      errors.push(`${errorJsonPath}: 不是有效 JSON (${error.message})`);
      continue;
    }

    const pyName = payload && payload.pyName;
    const failReason = payload && payload.failReason;
    if (typeof pyName !== "string" || path.basename(pyName) !== pyName || !pyName.endsWith(".py")) {
      errors.push(`${errorJsonPath}: pyName 必须是单个 .py 文件名`);
      continue;
    }
    if (typeof failReason !== "string") {
      errors.push(`${errorJsonPath}: failReason 必须是字符串`);
      continue;
    }

    const pyFiles = (await fs.readdir(caseDir, { withFileTypes: true }))
      .filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith(".py"))
      .map((entry) => entry.name);
    if (pyFiles.length !== 1 || pyFiles[0] !== pyName) {
      errors.push(`${caseDir}: 必须且只能包含与 pyName 一致的脚本，实际为 ${JSON.stringify(pyFiles)}`);
      continue;
    }

    const normalizedName = pyName.toLocaleLowerCase("en-US");
    if (seenNames.has(normalizedName)) {
      errors.push(`${caseDir}: pyName 与 ${seenNames.get(normalizedName)} 重复: ${pyName}`);
      continue;
    }
    seenNames.set(normalizedName, errorJsonPath);
    rows.push({ pyName, failReason });
  }

  rows.sort((left, right) => left.pyName.localeCompare(right.pyName, "en", { sensitivity: "base" }));
  return { rows, errors };
}


function addReportSheet(workbook, sheetName, rows) {
  const sheet = workbook.worksheets.add(sheetName);
  const matrix = [
    ["py脚本名称", "失败原因"],
    ...rows.map((row) => [safeCellText(row.pyName), safeCellText(row.failReason)]),
  ];
  const endRow = matrix.length;
  const usedRange = sheet.getRange(`A1:B${endRow}`);
  usedRange.values = matrix;

  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  const table = sheet.tables.add(`A1:B${endRow}`, true, sheetName === "卡点" ? "CardpointTable" : "RecycleTable");
  table.showFilterButton = true;

  const headerColor = sheetName === "卡点" ? "#B42318" : "#0F766E";
  sheet.getRange("A1:B1").format = {
    fill: headerColor,
    font: { bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    rowHeight: 28,
  };
  if (rows.length > 0) {
    const dataRange = sheet.getRange(`A2:B${endRow}`);
    dataRange.format = {
      font: { color: "#1F2937" },
      verticalAlignment: "top",
      wrapText: true,
    };
  }
  sheet.getRange(`A1:A${endRow}`).format.columnWidth = 42;
  sheet.getRange(`B1:B${endRow}`).format.columnWidth = 80;
  usedRange.format.autofitRows();
  return { sheet, endRow };
}


async function replaceOutput(tempOutput, finalOutput) {
  const backupOutput = `${finalOutput}.previous`;
  let hadExisting = false;
  try {
    hadExisting = await isFile(finalOutput);
    if (hadExisting) {
      await fs.rename(finalOutput, backupOutput);
    }
    await fs.rename(tempOutput, finalOutput);
    if (hadExisting) {
      await fs.rm(backupOutput, { force: true });
    }
  } catch (error) {
    if (!(await isFile(finalOutput)) && (await isFile(backupOutput))) {
      await fs.rename(backupOutput, finalOutput);
    }
    await fs.rm(tempOutput, { force: true });
    throw error;
  }
}


async function main() {
  const args = parseArgs(process.argv.slice(2));
  const dateStat = await fs.stat(args.dateDir);
  if (!dateStat.isDirectory()) {
    throw new Error(`工作目录不是目录: ${args.dateDir}`);
  }

  const { rows, errors } = await collectIgnoreRows(args.dateDir);
  if (errors.length > 0) {
    throw new Error(`ignore 数据校验失败，未生成 Excel:\n${errors.join("\n")}`);
  }
  if (rows.length === 0) {
    console.log(JSON.stringify({
      created: false,
      reason: "IGNORE_EMPTY",
      dateDir: args.dateDir,
      totalCount: 0,
    }, null, 2));
    return;
  }

  const cardpointRows = rows.filter((row) => !row.failReason.includes("回收"));
  const recycleRows = rows.filter((row) => row.failReason.includes("回收"));
  const emptyReasonCount = rows.filter((row) => row.failReason.trim() === "").length;
  const { SpreadsheetFile, Workbook } = await importArtifactTool(args.nodeModules);
  const workbook = Workbook.create();
  const reportSheets = [];
  if (cardpointRows.length > 0) {
    reportSheets.push(addReportSheet(workbook, "卡点", cardpointRows));
  }
  if (recycleRows.length > 0) {
    reportSheets.push(addReportSheet(workbook, "回收", recycleRows));
  }

  await fs.mkdir(args.previewDir, { recursive: true });
  const previewPaths = [];
  for (const report of reportSheets) {
    await workbook.inspect({
      kind: "table",
      sheetId: report.sheet.name,
      range: `A1:B${report.endRow}`,
      include: "values,formulas",
      tableMaxRows: Math.min(report.endRow, 20),
      tableMaxCols: 2,
      maxChars: 6000,
    });
    const preview = await workbook.render({
      sheetName: report.sheet.name,
      autoCrop: "all",
      scale: 1.5,
      format: "png",
    });
    const previewPath = path.join(args.previewDir, `${report.sheet.name}.png`);
    await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
    previewPaths.push(previewPath);
  }
  await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 100 },
    summary: "ignore report formula error scan",
    maxChars: 3000,
  });

  const dateName = path.basename(args.dateDir);
  const outputName = `${dateName}_失败_${rows.length}.xlsx`;
  const finalOutput = path.join(args.dateDir, outputName);
  const tempOutput = path.join(args.previewDir, `.${outputName}.${process.pid}.tmp.xlsx`);
  const stagingOutput = path.join(args.dateDir, `.${outputName}.${process.pid}.staging.xlsx`);
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(tempOutput);
  const exportedStat = await fs.stat(tempOutput);
  if (!exportedStat.isFile() || exportedStat.size === 0) {
    await fs.rm(tempOutput, { force: true });
    throw new Error(`导出的 Excel 无效或为空: ${tempOutput}`);
  }
  try {
    await fs.copyFile(tempOutput, stagingOutput);
    await replaceOutput(stagingOutput, finalOutput);
  } finally {
    await fs.rm(tempOutput, { force: true });
    await fs.rm(stagingOutput, { force: true });
  }

  console.log(JSON.stringify({
    created: true,
    output: finalOutput,
    totalCount: rows.length,
    cardpointCount: cardpointRows.length,
    recycleCount: recycleRows.length,
    emptyReasonCount,
    sheets: reportSheets.map((report) => report.sheet.name),
    previews: previewPaths,
  }, null, 2));
}


main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exitCode = 1;
});

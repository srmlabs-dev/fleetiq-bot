/**
 * ═══════════════════════════════════════════════════════════════
 *  FleetIQ Google Apps Script  —  Backend Sync
 *  Версия: 2.0
 *  Обслуживает:
 *    • FleetIQ v7.2  (fleet manager)  — полный дамп данных
 *    • FleetIQ Driver App             — рейсы и PTI отчёты
 * ═══════════════════════════════════════════════════════════════
 *
 *  УСТАНОВКА:
 *  1. Открой Google Sheets (новая таблица или существующая)
 *  2. Extensions → Apps Script
 *  3. Вставь весь этот код, удалив стандартную функцию myFunction
 *  4. Сохрани (Ctrl+S)
 *  5. Deploy → New deployment
 *     • Type: Web App
 *     • Execute as: Me
 *     • Who has access: Anyone
 *  6. Скопируй URL — вставь в оба приложения (Sync URL)
 *  7. При изменении кода — Deploy → Manage deployments → Edit → новая версия
 */

// ── КОНФИГУРАЦИЯ ──────────────────────────────────────────────
const CFG = {
  // Названия листов — можно переименовать
  SHEETS: {
    LOADS:        'Loads',
    FUEL:         'Fuel',
    SERVICE:      'Service',
    DEDUCTIONS:   'Deductions',
    PTI:          'PTI_Log',
    DRIVER_LOADS: 'Driver_Loads',
    ODO:          'Odometer',
    FLEET_SNAP:   'Fleet_Snapshot',
    LOG:          'Sync_Log',
    BOT_FEEDBACK: 'Bot_Feedback',
  },
  // Макс строк лога
  MAX_LOG_ROWS: 500,
};

// ── ENTRY POINTS ──────────────────────────────────────────────

/**
 * GET — читает данные обратно в приложение
 * Используется для загрузки последнего сохранённого состояния
 */
function doGet(e) {
  try {
    let type = e?.parameter?.type || 'fleet';
    let unit  = e?.parameter?.unit || '';

    if (type === 'driver') {
      return jsonResponse(getDriverData(unit));
    }
    return jsonResponse(getFleetData());

  } catch (err) {
    return jsonResponse({ error: err.message }, 500);
  }
}

/**
 * POST — принимает данные от приложений
 */
function doPost(e) {
  try {
    // GAS получает тело как postData.contents независимо от Content-Type
    let raw = '';
    if (e?.postData?.contents) {
      raw = e.postData.contents;
    } else if (e?.parameter) {
      raw = JSON.stringify(e.parameter);
    }
    if (!raw || raw === '{}') return jsonResponse({ error: 'Empty payload' });

    let data = JSON.parse(raw);
    let type = data.type || 'fleet_sync';

    logRequest(type, data.driver?.unitNumber || data.currentTruckId || '—');

    if (type === 'bot_feedback') {
      handleBotFeedback(data);
      return jsonResponse({ status: 'ok', type: 'bot_feedback' });
    }

    if (type === 'driver_report') {
      handleDriverReport(data);
      return jsonResponse({ status: 'ok', type: 'driver_report' });
    }

    if (type === 'pti_report') {
      handlePTIReport(data);
      return jsonResponse({ status: 'ok', type: 'pti_report' });
    }

    // fleet_sync (FleetIQ v7.2)
    handleFleetSync(data);
    // Возвращаем актуальные данные из таблицы (двусторонняя синхронизация)
    return jsonResponse({ status: 'ok', type: 'fleet_sync', data: getFleetData() });

  } catch (err) {
    logError(err.message);
    return jsonResponse({ error: err.message }, 500);
  }
}

// ── FLEET MANAGER SYNC ────────────────────────────────────────

function handleFleetSync(data) {
  let ss = SpreadsheetApp.getActiveSpreadsheet();

  // 1. Снимок всего флота (Fleet_Snapshot)
  writeFleetSnapshot(ss, data);

  // 2. Рейсы по каждому траку
  if (data.truckData) {
    Object.entries(data.truckData).forEach(([truckId, td]) => {
      let truck = (data.trucks || []).find(t => t.id === truckId) || { make: '?', unitNumber: truckId };
      let label = `${truck.make} #${truck.unitNumber || truckId}`;

      if (td.loads?.length)    writeLoads(ss, td.loads, label);
      if (td.fuel?.length)     writeFuel(ss, td.fuel, label);
      if (td.services?.length) writeServices(ss, td.services, label);
    });
  }

  // 3. Еженедельные вычеты
  if (data.weeklyDeductions?.length) {
    writeDeductions(ss, data.weeklyDeductions, data.trucks || []);
  }
}

function writeFleetSnapshot(ss, data) {
  let sh = getOrCreateSheet(ss, CFG.SHEETS.FLEET_SNAP);
  sh.clearContents();

  // Заголовок
  let now = new Date().toLocaleString('en-US', { timeZone: 'America/Chicago' });
  sh.getRange(1, 1, 1, 2).setValues([['Last Sync', now]]);
  sh.getRange(1, 1).setFontWeight('bold').setFontColor('#f59e0b');

  // Сводка по тракам
  let row = 3;
  sh.getRange(row, 1, 1, 8).setValues([[
    'Truck', 'Unit', 'Year', 'Dispatch%', 'Maint$/mi', 'Purchase$', 'Week Type', 'Active'
  ]]).setFontWeight('bold').setBackground('#1a2235').setFontColor('#64748b');
  row++;

  (data.trucks || []).forEach(t => {
    sh.getRange(row, 1, 1, 8).setValues([[
      `${t.make} ${t.model}`, t.unitNumber || '', t.year || '',
      t.dispatchPercent || '', t.maintenanceRate || '',
      t.purchaseCost || '', t.weekType || '', t.isActive ? 'Yes' : 'No'
    ]]);
    row++;
  });

  // Сводка по водителям
  row += 2;
  sh.getRange(row, 1, 1, 5).setValues([[
    'Driver', 'Pay Type', 'Rate', 'CPM Base', 'Trucks'
  ]]).setFontWeight('bold').setBackground('#1a2235').setFontColor('#64748b');
  row++;

  (data.drivers || []).forEach(d => {
    let rate = d.payType === 'gross_percent' ? `${d.rate}%` : `$${d.rate}/mi`;
    sh.getRange(row, 1, 1, 5).setValues([[
      d.name, d.payType, rate, d.cpmBase || '',
      (d.assignedTruckIds || []).join(', ') || 'All'
    ]]);
    row++;
  });

  autoResizeColumns(sh, 8);
}

function writeLoads(ss, loads, truckLabel) {
  let sh = getOrCreateSheet(ss, CFG.SHEETS.LOADS);
  ensureLoadsHeader(sh);

  let existing = getExistingIds(sh, 1); // col A = load ID + truck
  let newRows = [];

  loads.forEach(l => {
    let key = `${l.id}`;
    if (existing.has(key)) return; // не дублируем
    newRows.push([
      l.id || '',
      l.loadId || '',
      truckLabel,
      l.status || 'normal',
      l.pickup || '',
      l.delivery || '',
      nf(l.loadedMiles), nf(l.deadheadMiles), nf(l.totalMiles),
      nf(l.gross), nf(l.dispatchPercent), nf(l.dispatchFee),
      nf(l.truckNet), nf(l.driverPay), nf(l.ownerNet),
      nf(l.fuelActual), nf(l.defActual), nf(l.tolls),
      nf(l.lumper), nf(l.rebate),
      l.notes || '',
      new Date().toISOString()
    ]);
  });

  if (newRows.length) appendRows(sh, newRows);
  autoResizeColumns(sh, 22);
}

function ensureLoadsHeader(sh) {
  if (sh.getLastRow() > 0) return;
  sh.getRange(1, 1, 1, 22).setValues([[
    'ID', 'Load ID', 'Truck', 'Status', 'Pickup', 'Delivery',
    'Loaded mi', 'Dead mi', 'Total mi',
    'Gross', 'Dispatch%', 'Dispatch$', 'Truck Net',
    'Driver Pay', 'Owner Net',
    'Fuel$', 'DEF$', 'Tolls', 'Lumper', 'Rebate',
    'Notes', 'Synced At'
  ]]).setFontWeight('bold').setBackground('#111827').setFontColor('#f59e0b');
  sh.setFrozenRows(1);
}

function writeFuel(ss, logs, truckLabel) {
  let sh = getOrCreateSheet(ss, CFG.SHEETS.FUEL);
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, 10).setValues([[
      'ID', 'Truck', 'Date', 'Odometer', 'Gallons', 'Fuel$', 'DEF Gal', 'DEF$', 'MPG', 'Location'
    ]]).setFontWeight('bold').setBackground('#111827').setFontColor('#f59e0b');
    sh.setFrozenRows(1);
  }
  let existing = getExistingIds(sh, 1);
  let newRows = logs
    .filter(l => !existing.has(l.id))
    .map(l => [
      l.id, truckLabel, l.date || '', nf(l.odometer),
      nf(l.fuelGallons), nf(l.fuelCost),
      nf(l.defGallons), nf(l.defCost),
      nf(l.mpg), l.location || ''
    ]);
  if (newRows.length) appendRows(sh, newRows);
  autoResizeColumns(sh, 10);
}

function writeServices(ss, services, truckLabel) {
  let sh = getOrCreateSheet(ss, CFG.SHEETS.SERVICE);
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, 8).setValues([[
      'ID', 'Truck', 'Date', 'Odometer', 'Amount$', 'Category', 'Description', 'From Fund'
    ]]).setFontWeight('bold').setBackground('#111827').setFontColor('#f59e0b');
    sh.setFrozenRows(1);
  }
  let existing = getExistingIds(sh, 1);
  let newRows = services
    .filter(s => !existing.has(s.id))
    .map(s => [
      s.id, truckLabel, s.date || '', nf(s.odometer),
      nf(s.amount), s.category || '', s.description || '',
      s.fromFund ? 'Yes' : 'No'
    ]);
  if (newRows.length) appendRows(sh, newRows);
  autoResizeColumns(sh, 8);
}

function writeDeductions(ss, deductions, trucks) {
  let sh = getOrCreateSheet(ss, CFG.SHEETS.DEDUCTIONS);
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, 6).setValues([[
      'ID', 'Truck', 'Week', 'Item', 'Amount$', 'Category'
    ]]).setFontWeight('bold').setBackground('#111827').setFontColor('#f59e0b');
    sh.setFrozenRows(1);
  }
  let existing = getExistingIds(sh, 1);
  let newRows = [];
  deductions.forEach(d => {
    let truck = trucks.find(t => t.id === d.truckId) || { make: '?', unitNumber: d.truckId };
    let label = `${truck.make} #${truck.unitNumber}`;
    (d.items || []).forEach((item, i) => {
      let key = `${d.id}_${i}`;
      if (!existing.has(key)) {
        newRows.push([key, label, d.weekKey || '', item.name || '', item.amount || 0, item.category || '']);
      }
    });
  });
  if (newRows.length) appendRows(sh, newRows);
  autoResizeColumns(sh, 6);
}

// ── DRIVER APP HANDLERS ───────────────────────────────────────

function handleDriverReport(data) {
  let ss = SpreadsheetApp.getActiveSpreadsheet();
  let driverName = data.driver?.name || 'Unknown';
  let unitNum    = data.driver?.unitNumber || '?';
  let label      = `${driverName} / Unit ${unitNum}`;

  // Рейсы водителя
  if (data.loads?.length) {
    writeDriverLoads(ss, data.loads, label);
  }

  // PTI если пришли вместе
  if (data.ptiLog?.length) {
    data.ptiLog.forEach(p => writePTIEntry(ss, p, label));
  }
}

function handlePTIReport(data) {
  let ss = SpreadsheetApp.getActiveSpreadsheet();
  let label = `${data.driver?.name || '?'} / Unit ${data.driver?.unitNumber || '?'}`;
  if (data.pti) writePTIEntry(ss, data.pti, label);
  // Одометр отдельным листом
  if (data.pti?.odometer) writeOdometer(ss, data.pti, label);
}

function writeDriverLoads(ss, loads, driverLabel) {
  let sh = getOrCreateSheet(ss, CFG.SHEETS.DRIVER_LOADS);
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, 13).setValues([[
      'ID', 'Driver/Unit', 'Load ID', 'Pickup', 'Delivery',
      'Loaded mi', 'Dead mi', 'Total mi',
      'Gross$', 'Driver Pay$', '$/mi', 'Notes', 'Status'
    ]]).setFontWeight('bold').setBackground('#111827').setFontColor('#f59e0b');
    sh.setFrozenRows(1);
  }
  let existing = getExistingIds(sh, 1);
  let newRows = loads
    .filter(l => !existing.has(l.id))
    .map(l => {
      let tmi = nf(l.loadedMiles) + nf(l.deadMiles);
      let ppm = tmi ? nf((nf(l.driverPay)) / tmi) : 0;
      return [
        l.id, driverLabel, l.loadId || '',
        l.pickup || '', l.delivery || '',
        nf(l.loadedMiles), nf(l.deadMiles),
        nf(l.totalMiles) || tmi,
        nf(l.gross), nf(l.driverPay), ppm,
        l.notes || '', l.status || 'active'
      ];
    });
  if (newRows.length) appendRows(sh, newRows);
  autoResizeColumns(sh, 12);
}

function writePTIEntry(ss, pti, driverLabel) {
  let sh = getOrCreateSheet(ss, CFG.SHEETS.PTI);
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, 8).setValues([[
      'ID', 'Driver/Unit', 'Date', 'Type', 'Passed',
      'Issues', 'Odometer', 'Items (JSON)'
    ]]).setFontWeight('bold').setBackground('#111827').setFontColor('#f59e0b');
    sh.setFrozenRows(1);
  }
  let existing = getExistingIds(sh, 1);
  if (existing.has(pti.id)) return;
  let failedItems = (pti.items || []).filter(x => x.state === 'fail').map(x => x.name).join(', ');
  appendRows(sh, [[
    pti.id, driverLabel, pti.date || '',
    pti.type || 'daily',
    pti.passed ? 'YES' : 'NO',
    pti.issues || failedItems || '',
    pti.odometer || 0,
    JSON.stringify(pti.items || [])
  ]]);
  autoResizeColumns(sh, 7);
  // Цвет строки по результату
  let lastRow = sh.getLastRow();
  let color = pti.passed ? '#0d2b1a' : '#2b0d0d';
  sh.getRange(lastRow, 1, 1, 7).setBackground(color);
}

function writeOdometer(ss, pti, driverLabel) {
  let sh = getOrCreateSheet(ss, CFG.SHEETS.ODO);
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, 5).setValues([[
      'Driver/Unit', 'Date', 'Odometer', 'PTI Type', 'PTI ID'
    ]]).setFontWeight('bold').setBackground('#111827').setFontColor('#f59e0b');
    sh.setFrozenRows(1);
  }
  // Dedup by PTI ID — prevent duplicate odometer entries
  let existing = getExistingIds(sh, 5); // col E = PTI ID
  if (pti.id && existing.has(pti.id)) return;
  appendRows(sh, [[driverLabel, pti.date || '', pti.odometer || 0, pti.type || 'daily', pti.id || '']]);
  autoResizeColumns(sh, 5);
}

// ── DEBUG ──────────────────────────────────────────────────────

/**
 * Запусти эту функцию из редактора чтобы увидеть что в таблице
 */
function debugDriverLoads() {
  let ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(CFG.SHEETS.DRIVER_LOADS);
  if (!sh) { Logger.log('❌ Лист Driver_Loads не найден'); return; }
  Logger.log('✅ Лист найден. Строк: ' + sh.getLastRow());
  if (sh.getLastRow() < 2) { Logger.log('⚠️ Данных нет (только заголовок или пусто)'); return; }
  let rows = sh.getRange(1, 1, sh.getLastRow(), 12).getValues();
  rows.forEach((r, i) => Logger.log(`Строка ${i+1}: ${JSON.stringify(r)}`));
  Logger.log('--- getFleetData результат ---');
  let result = getFleetData();
  Logger.log('driverLoads count: ' + result.driverLoads.length);
  Logger.log('ptiLog count: ' + result.ptiLog.length);
  Logger.log(JSON.stringify(result.driverLoads));
}

// ── GET DATA (для чтения обратно в приложение) ────────────────

function getFleetData() {
  let ss = SpreadsheetApp.getActiveSpreadsheet();

  // Читаем рейсы водителей из Driver_Loads
  let driverLoads = [];
  let dlSh = ss.getSheetByName(CFG.SHEETS.DRIVER_LOADS);
  if (dlSh && dlSh.getLastRow() > 1) {
    let rows = dlSh.getRange(2, 1, dlSh.getLastRow()-1, 12).getValues();
    rows.forEach(r => {
      if (!r[0]) return;
      driverLoads.push({
        id:         String(r[0]),
        driverUnit: String(r[1]),
        loadId:     String(r[2]),
        pickup:     r[3] ? (r[3] instanceof Date ? r[3].toISOString().slice(0,10) : String(r[3])) : '',
        delivery:   r[4] ? (r[4] instanceof Date ? r[4].toISOString().slice(0,10) : String(r[4])) : '',
        loadedMiles: safeNum(r[5]),
        deadMiles:   safeNum(r[6]),
        totalMiles:  safeNum(r[7]),
        gross:       safeNum(r[8]),
        driverPay:   safeNum(r[9]),
        ppm:         safeNum(r[10]),
        notes:       String(r[11]||'')
      });
    });
  }

  // Читаем PTI лог
  let ptiLog = [];
  let ptiSh = ss.getSheetByName(CFG.SHEETS.PTI);
  if (ptiSh && ptiSh.getLastRow() > 1) {
    let rows = ptiSh.getRange(2, 1, ptiSh.getLastRow()-1, 7).getValues();
    rows.forEach(r => {
      if (!r[0]) return;
      ptiLog.push({
        id:        String(r[0]),
        driverUnit:String(r[1]),
        date:      r[2] ? (r[2] instanceof Date ? r[2].toISOString().slice(0,10) : String(r[2])) : '',
        type:      String(r[3]||'daily'),
        passed:    String(r[4]).toUpperCase()==='YES',
        issues:    String(r[5]||''),
        odometer:  Number(r[6])||0
      });
    });
  }

  // Читаем одометр
  let odoLog = [];
  let odoSh = ss.getSheetByName(CFG.SHEETS.ODO);
  if (odoSh && odoSh.getLastRow() > 1) {
    let rows = odoSh.getRange(2, 1, odoSh.getLastRow()-1, 4).getValues();
    rows.forEach(r => {
      if (!r[0]) return;
      odoLog.push({
        driverUnit: String(r[0]),
        date:       r[1] ? (r[1] instanceof Date ? r[1].toISOString().slice(0,10) : String(r[1])) : '',
        odometer:   Number(r[2])||0,
        type:       String(r[3]||'daily')
      });
    });
  }

  // Последний sync из лога
  let logSh = ss.getSheetByName(CFG.SHEETS.LOG);
  let lastSync = null;
  if (logSh && logSh.getLastRow() > 1) {
    let row = logSh.getRange(logSh.getLastRow(), 1, 1, 3).getValues()[0];
    lastSync = { time: String(row[0]), type: String(row[1]), unit: String(row[2]) };
  }

  // Формируем список рейсов готовых к автоимпорту
  // Парсим unitNumber из driverUnit строки вида "Tom / Unit 2323"
  let pendingImport = driverLoads.map(l => {
    let unitMatch = String(l.driverUnit||'').match(/Unit\s+(\S+)/i);
    let unitNumber = unitMatch ? unitMatch[1] : '';
    let driverName = String(l.driverUnit||'').split('/')[0].trim();
    return { ...l, unitNumber, driverName };
  }).filter(l => l.unitNumber && l.loadId);

  return {
    status: 'ok',
    lastSync,
    driverLoads,
    pendingImport,
    ptiLog,
    odoLog,
    sheets: Object.values(CFG.SHEETS)
  };
}

function getDriverData(unitNumber) {
  // Возвращает рейсы водителя по Unit Number (для сверки)
  let ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(CFG.SHEETS.DRIVER_LOADS);
  if (!sh || sh.getLastRow() < 2) return { loads: [] };
  let rows = sh.getRange(2, 1, sh.getLastRow() - 1, 12).getValues();
  let loads = rows
    .filter(r => r[0] && (!unitNumber || String(r[1]).includes(unitNumber)))
    .map(r => ({
      id:          String(r[0]),
      driverUnit:  String(r[1]),
      loadId:      String(r[2]),
      pickup:      r[3] instanceof Date ? r[3].toISOString().slice(0,10) : String(r[3]||''),
      delivery:    r[4] instanceof Date ? r[4].toISOString().slice(0,10) : String(r[4]||''),
      loadedMiles: safeNum(r[5]),
      deadMiles:   safeNum(r[6]),
      totalMiles:  safeNum(r[7]),
      gross:       safeNum(r[8]),
      driverPay:   safeNum(r[9]),
      ppm:         safeNum(r[10]),
      notes:       String(r[11]||''),
      status:      String(r[12]||'active'),
      synced:      true
    }));
  return { status: 'ok', loads };
}

// ── BOT FEEDBACK ─────────────────────────────────────────────

function handleBotFeedback(data) {
  let ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = getOrCreateSheet(ss, CFG.SHEETS.BOT_FEEDBACK);

  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, 14).setValues([[
      'Timestamp', 'Telegram ID', 'Username', 'Full Name',
      'Type', 'Priority', 'Module', 'Summary',
      'Message', 'Engineering Prompt', 'Bot Response',
      'App Version', 'Status', 'Notes'
    ]]).setFontWeight('bold').setBackground('#111827').setFontColor('#f59e0b');
    sh.setFrozenRows(1);
  }

  let row = [
    data.timestamp || new Date().toISOString(),
    data.telegram_id || '',
    data.username || '',
    data.full_name || '',
    data.fb_type || data.msg_type || '',
    data.priority || 'medium',
    data.module || 'other',
    data.summary || '',
    data.message || '',
    data.engineering_prompt || '',
    data.bot_response || '',
    data.app_version || '',
    'new',
    ''
  ];

  appendRows(sh, [row]);

  // Color by priority
  let lastRow = sh.getLastRow();
  let colors = { high: '#2b0d0d', medium: '#1a2235', low: '#0d2b1a' };
  let color = colors[data.priority] || '#1a2235';
  sh.getRange(lastRow, 1, 1, 14).setBackground(color);

  autoResizeColumns(sh, 14);
}

// ── HELPERS ───────────────────────────────────────────────────

function getOrCreateSheet(ss, name) {
  let sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    // Тёмный фон для нового листа
    sh.setTabColor('#1a2235');
  }
  return sh;
}

function getExistingIds(sh, col) {
  let set = new Set();
  let last = sh.getLastRow();
  if (last < 2) return set;
  let vals = sh.getRange(2, col, last - 1, 1).getValues();
  vals.forEach(r => { if (r[0]) set.add(String(r[0])); });
  return set;
}

function appendRows(sh, rows) {
  if (!rows.length) return;
  let last = sh.getLastRow();
  sh.getRange(last + 1, 1, rows.length, rows[0].length).setValues(rows);
}

function autoResizeColumns(sh, count) {
  try { sh.autoResizeColumns(1, count); } catch (e) {}
}

function logRequest(type, unit) {
  let ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = getOrCreateSheet(ss, CFG.SHEETS.LOG);
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, 3).setValues([['Time', 'Type', 'Unit/Truck']]).setFontWeight('bold');
  }
  let now = new Date().toLocaleString('en-US', { timeZone: 'America/Chicago' });
  appendRows(sh, [[now, type, unit]]);
  // Чистим лог если слишком длинный
  let rows = sh.getLastRow();
  if (rows > CFG.MAX_LOG_ROWS + 1) {
    sh.deleteRows(2, rows - CFG.MAX_LOG_ROWS);
  }
}

function logError(msg) {
  try {
    let ss = SpreadsheetApp.getActiveSpreadsheet();
    let sh = getOrCreateSheet(ss, CFG.SHEETS.LOG);
    let now = new Date().toLocaleString('en-US', { timeZone: 'America/Chicago' });
    appendRows(sh, [[now, 'ERROR', msg]]);
  } catch (e) {}
}

function safeNum(v) {
  if (typeof v === 'number') return v;
  let s = String(v||'').replace(',', '.');
  return parseFloat(s) || 0;
}
function n(v){ return safeNum(v); }
function nf(v){ return parseFloat((safeNum(v)).toFixed(4)); }

function jsonResponse(obj, code) {
  let output = ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
  return output;
}

// ── WEEKLY SUMMARY (опционально — запускать по расписанию) ────

/**
 * Создаёт еженедельный сводный отчёт
 * Можно настроить триггер: Triggers → Add trigger → weeklySummary → Week timer
 */
function weeklySummary() {
  let ss = SpreadsheetApp.getActiveSpreadsheet();
  let loadsSh = ss.getSheetByName(CFG.SHEETS.LOADS);
  if (!loadsSh || loadsSh.getLastRow() < 2) return;

  // Определяем прошлую неделю
  let now = new Date();
  let day = now.getDay();
  let lastMon = new Date(now); lastMon.setDate(now.getDate() - day - 6);
  let lastSun = new Date(now); lastSun.setDate(now.getDate() - day);
  let wkStart = lastMon.toISOString().slice(0, 10);
  let wkEnd   = lastSun.toISOString().slice(0, 10);

  let rows = loadsSh.getRange(2, 1, loadsSh.getLastRow() - 1, 22).getValues();
  let weekRows = rows.filter(r => {
    let d = String(r[4]); // Pickup date col E
    return d >= wkStart && d <= wkEnd;
  });

  if (!weekRows.length) return;

  // Группируем по траку
  let byTruck = {};
  weekRows.forEach(r => {
    let truck = String(r[2]);
    if (!byTruck[truck]) byTruck[truck] = { gross:0, truckNet:0, driverPay:0, ownerNet:0, miles:0, loads:0 };
    byTruck[truck].gross     += Number(r[9]  || 0);
    byTruck[truck].truckNet  += Number(r[12] || 0);
    byTruck[truck].driverPay += Number(r[13] || 0);
    byTruck[truck].ownerNet  += Number(r[14] || 0);
    byTruck[truck].miles     += Number(r[8]  || 0);
    byTruck[truck].loads     += 1;
  });

  // Создаём лист Summary_YYYY-WW
  let wkLabel = `Summary_${wkStart}`;
  let sh = getOrCreateSheet(ss, wkLabel);
  sh.clearContents();

  sh.getRange(1,1,1,2).setValues([[`Weekly Summary`, `${wkStart} → ${wkEnd}`]]);
  sh.getRange(1,1).setFontSize(14).setFontWeight('bold').setFontColor('#f59e0b');

  sh.getRange(3,1,1,7).setValues([[
    'Truck','Loads','Miles','Gross','Truck Net','Driver Pay','Owner Net'
  ]]).setFontWeight('bold').setBackground('#111827').setFontColor('#64748b');

  let row = 4;
  let totals = { loads:0, miles:0, gross:0, truckNet:0, driverPay:0, ownerNet:0 };
  Object.entries(byTruck).forEach(([truck, d]) => {
    sh.getRange(row,1,1,7).setValues([[
      truck, d.loads, d.miles,
      `$${d.gross.toFixed(2)}`, `$${d.truckNet.toFixed(2)}`,
      `$${d.driverPay.toFixed(2)}`, `$${d.ownerNet.toFixed(2)}`
    ]]);
    totals.loads    += d.loads;
    totals.miles    += d.miles;
    totals.gross    += d.gross;
    totals.truckNet += d.truckNet;
    totals.driverPay+= d.driverPay;
    totals.ownerNet += d.ownerNet;
    row++;
  });

  // Итого
  sh.getRange(row+1,1,1,7).setValues([[
    'TOTAL', totals.loads, totals.miles,
    `$${totals.gross.toFixed(2)}`, `$${totals.truckNet.toFixed(2)}`,
    `$${totals.driverPay.toFixed(2)}`, `$${totals.ownerNet.toFixed(2)}`
  ]]).setFontWeight('bold').setBackground('#1a2235').setFontColor('#f59e0b');

  autoResizeColumns(sh, 7);
  SpreadsheetApp.flush();
}

// ── ТЕСТ (запускать вручную из Apps Script editor) ─────────────

function testSetup() {
  let ss = SpreadsheetApp.getActiveSpreadsheet();
  Logger.log('Spreadsheet: ' + ss.getName());
  Logger.log('Sheets: ' + ss.getSheets().map(s => s.getName()).join(', '));

  // Создаём все нужные листы
  Object.values(CFG.SHEETS).forEach(name => {
    getOrCreateSheet(ss, name);
    Logger.log('Sheet ready: ' + name);
  });

  Logger.log('✅ Setup complete. Deploy as Web App to get sync URL.');
}
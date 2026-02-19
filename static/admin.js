(function () {
  const bookingsRowsEl = document.getElementById('bookings-rows');
  const bookingsFeedbackEl = document.getElementById('bookings-feedback');
  const filterDateEl = document.getElementById('filter-date');
  const filterStatusEl = document.getElementById('filter-status');
  const filterTrailerEl = document.getElementById('filter-trailer');

  const kpiTodayEl = document.getElementById('kpi-today');
  const kpiPendingEl = document.getElementById('kpi-pending');
  const kpiConfirmedEl = document.getElementById('kpi-confirmed');
  const kpiCancelledEl = document.getElementById('kpi-cancelled');

  const blocksRowsEl = document.getElementById('blocks-rows');
  const blocksFeedbackEl = document.getElementById('blocks-feedback');
  const blockFormEl = document.getElementById('block-form');
  const blockTrailerEl = document.getElementById('block-trailer');
  const blockStartEl = document.getElementById('block-start');
  const blockEndEl = document.getElementById('block-end');
  const blockReasonEl = document.getElementById('block-reason');

  const testBookingsRowsEl = document.getElementById('test-bookings-rows');
  const testBookingsFeedbackEl = document.getElementById('test-bookings-feedback');
  const latestTestBookingResultEl = document.getElementById('latest-test-booking-result');
  const testBookingFormEl = document.getElementById('test-booking-form');
  const testSmsToEl = document.getElementById('test-sms-to');
  const testTrailerTypeEl = document.getElementById('test-trailer-type');
  const testDateEl = document.getElementById('test-date');
  const testRentalTypeEl = document.getElementById('test-rental-type');

  const refreshBookingsBtn = document.getElementById('refresh-bookings');
  const refreshBlocksBtn = document.getElementById('refresh-blocks');
  const refreshTestBookingsBtn = document.getElementById('refresh-test-bookings');
  const runTestBookingsNowBtn = document.getElementById('run-test-bookings-now');

  const state = {
    bookings: [],
    blocks: [],
    testBookings: [],
    countdownTimerId: null
  };

  function getAdminToken() {
    const key = 'adminApiToken';
    const existing = window.localStorage.getItem(key);
    if (existing) return existing;
    const entered = window.prompt('Ange ADMIN_TOKEN för admin-API:');
    if (!entered) return '';
    const trimmed = entered.trim();
    if (!trimmed) return '';
    window.localStorage.setItem(key, trimmed);
    return trimmed;
  }

  async function adminFetch(path, options) {
    const token = getAdminToken();
    const requestOptions = Object.assign({ cache: 'no-store' }, options || {});
    requestOptions.headers = Object.assign({}, requestOptions.headers || {});
    if (token) {
      requestOptions.headers['X-Admin-Token'] = token;
    }
    const response = await fetch(path, requestOptions);
    if (response.status === 401) {
      window.localStorage.removeItem('adminApiToken');
    }
    return response;
  }

  function formatTrailer(type) {
    if (type === 'GALLER') return 'Gallersläp';
    if (type === 'KAP' || type === 'KAPS') return 'Kåpsläp';
    return type || '-';
  }

  function formatStatus(status) {
    if (status === 'PENDING_PAYMENT') return 'Pending';
    if (status === 'CONFIRMED') return 'Confirmed';
    if (status === 'CANCELLED') return 'Cancelled';
    if (status === 'PENDING') return 'Pending';
    if (status === 'PAID') return 'PAID';
    return status || '-';
  }

  function statusClass(status) {
    if (status === 'PENDING_PAYMENT' || status === 'PENDING') return 'status-pending';
    if (status === 'CONFIRMED' || status === 'PAID') return 'status-confirmed';
    if (status === 'CANCELLED') return 'status-cancelled';
    return '';
  }

  function toDateOnly(isoDatetime) {
    if (!isoDatetime || isoDatetime.length < 10) return '';
    return isoDatetime.slice(0, 10);
  }

  function formatDateTime(isoDatetime) {
    if (!isoDatetime) return '-';
    const value = isoDatetime.length >= 16 ? isoDatetime.slice(0, 16) : isoDatetime;
    return value.replace('T', ' ');
  }

  function formatTimeRange(startDt, endDt) {
    const start = formatDateTime(startDt);
    const end = formatDateTime(endDt);
    if (start === '-' || end === '-') return '-';
    return start + ' - ' + end;
  }

  function formatCountdown(deleteAt) {
    if (!deleteAt) return '-';
    const deleteMs = Date.parse(deleteAt);
    if (!Number.isFinite(deleteMs)) return '-';
    const diff = Math.max(0, Math.floor((deleteMs - Date.now()) / 1000));
    const minutes = Math.floor(diff / 60);
    const seconds = diff % 60;
    if (diff <= 0) return 'Utgår nu';
    return String(minutes).padStart(2, '0') + ':' + String(seconds).padStart(2, '0');
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function computeFilteredBookings() {
    const selectedDate = filterDateEl.value;
    const selectedTrailer = filterTrailerEl.value;
    const selectedStatus = filterStatusEl.value;

    let rows = state.bookings.slice();
    if (selectedDate) {
      rows = rows.filter((row) => toDateOnly(row.startDt) === selectedDate);
    }
    if (selectedTrailer !== 'ALL') {
      rows = rows.filter((row) => row.trailerType === selectedTrailer);
    }
    if (selectedStatus !== 'ALL') {
      rows = rows.filter((row) => row.status === selectedStatus);
    }
    return rows;
  }

  function updateKpis() {
    const todayDate = new Date().toISOString().slice(0, 10);
    const selectedDate = filterDateEl.value;
    const selectedTrailer = filterTrailerEl.value;

    const forStatusCounts = state.bookings.filter((row) => {
      if (selectedDate && toDateOnly(row.startDt) !== selectedDate) return false;
      if (selectedTrailer !== 'ALL' && row.trailerType !== selectedTrailer) return false;
      return true;
    });

    const todayCount = state.bookings.filter((row) => toDateOnly(row.startDt) === todayDate).length;
    const pendingCount = forStatusCounts.filter((row) => row.status === 'PENDING_PAYMENT').length;
    const confirmedCount = forStatusCounts.filter((row) => row.status === 'CONFIRMED').length;
    const cancelledCount = forStatusCounts.filter((row) => row.status === 'CANCELLED').length;

    kpiTodayEl.textContent = String(todayCount);
    kpiPendingEl.textContent = String(pendingCount);
    kpiConfirmedEl.textContent = String(confirmedCount);
    kpiCancelledEl.textContent = String(cancelledCount);
  }

  function renderBookings() {
    const rows = computeFilteredBookings();
    updateKpis();

    if (!rows.length) {
      bookingsRowsEl.innerHTML = '<tr><td colspan="6">Inga bokningar matchar filtret.</td></tr>';
      bookingsFeedbackEl.textContent = '0 bokningar visas.';
      return;
    }

    bookingsRowsEl.innerHTML = rows.map((row) => {
      const reference = row.bookingReference || ('Bokning #' + row.bookingId);
      const detailsHref = '/confirm?bookingId=' + encodeURIComponent(row.bookingId);
      return '<tr>' +
        '<td>' + escapeHtml(reference) + '</td>' +
        '<td>' + escapeHtml(formatTrailer(row.trailerType)) + '</td>' +
        '<td>' + escapeHtml(formatTimeRange(row.startDt, row.endDt)) + '</td>' +
        '<td><span class="status-pill ' + escapeHtml(statusClass(row.status)) + '">' + escapeHtml(formatStatus(row.status)) + '</span></td>' +
        '<td>' + escapeHtml(String(row.price)) + ' kr</td>' +
        '<td><a class="link" href="' + detailsHref + '" target="_blank" rel="noopener">Öppna detaljer</a></td>' +
      '</tr>';
    }).join('');
    bookingsFeedbackEl.textContent = rows.length + ' bokningar visas.';
  }

  function renderTestBookings() {
    const rows = state.testBookings.slice().sort((a, b) => String(b.createdAt || '').localeCompare(String(a.createdAt || '')));
    if (!rows.length) {
      testBookingsRowsEl.innerHTML = '<tr><td colspan="5">Inga aktiva testbokningar.</td></tr>';
      return;
    }
    testBookingsRowsEl.innerHTML = rows.map((row) => {
      const preview = row.receiptPreview;
      const previewHtml = preview
        ? '<div><strong>' + escapeHtml(preview.bookingReference || '-') + '</strong><br>' +
          'Status: ' + escapeHtml(preview.status || '-') + '<br>' +
          'Släp: ' + escapeHtml(formatTrailer(preview.trailerType || '-')) + '<br>' +
          'Pris: ' + escapeHtml(String(preview.price || '-')) + ' kr</div>'
        : '-';
      return '<tr>' +
        '<td>' + escapeHtml(row.bookingReference || ('TEST-' + row.id)) + '</td>' +
        '<td><span class="status-pill ' + escapeHtml(statusClass(row.status)) + '">' + escapeHtml(formatStatus(row.status)) + '</span></td>' +
        '<td>' + escapeHtml(formatCountdown(row.deleteAt)) + '</td>' +
        '<td>' + escapeHtml(formatDateTime(row.autoPaidAt)) + '</td>' +
        '<td>' + previewHtml + '</td>' +
      '</tr>';
    }).join('');
  }

  async function fetchBookings() {
    bookingsFeedbackEl.textContent = 'Laddar bokningar...';
    try {
      const response = await adminFetch('/api/admin/bookings');
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data && data.error ? data.error : 'Kunde inte ladda bokningar');
      }
      state.bookings = Array.isArray(data.bookings) ? data.bookings : [];
      renderBookings();
    } catch (error) {
      bookingsFeedbackEl.textContent = 'Fel: ' + error.message;
      bookingsRowsEl.innerHTML = '';
      updateKpis();
    }
  }

  function renderBlocks() {
    const rows = state.blocks.slice().sort((a, b) => {
      return String(a.startDatetime || '').localeCompare(String(b.startDatetime || ''));
    });

    if (!rows.length) {
      blocksRowsEl.innerHTML = '<tr><td colspan="6">Inga aktiva blockeringar.</td></tr>';
      return;
    }

    blocksRowsEl.innerHTML = rows.map((row) => {
      const reason = row.reason || '-';
      return '<tr>' +
        '<td>' + escapeHtml(String(row.id)) + '</td>' +
        '<td>' + escapeHtml(formatTrailer(row.trailerType)) + '</td>' +
        '<td>' + escapeHtml(formatDateTime(row.startDatetime)) + '</td>' +
        '<td>' + escapeHtml(formatDateTime(row.endDatetime)) + '</td>' +
        '<td>' + escapeHtml(reason) + '</td>' +
        '<td><button type="button" class="button delete-block" data-id="' + escapeHtml(String(row.id)) + '">Ta bort</button></td>' +
      '</tr>';
    }).join('');
  }

  async function fetchBlocks() {
    blocksFeedbackEl.textContent = 'Laddar blockeringar...';
    try {
      const response = await adminFetch('/api/admin/blocks');
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data && data.error ? data.error : 'Kunde inte ladda blockeringar');
      }
      state.blocks = Array.isArray(data.blocks) ? data.blocks : [];
      renderBlocks();
      blocksFeedbackEl.textContent = state.blocks.length + ' blockeringar hämtade.';
    } catch (error) {
      blocksFeedbackEl.textContent = 'Fel: ' + error.message;
      blocksRowsEl.innerHTML = '';
    }
  }

  async function fetchTestBookings() {
    testBookingsFeedbackEl.textContent = 'Laddar testbokningar...';
    try {
      const response = await adminFetch('/api/admin/test-bookings');
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data && data.error ? data.error : 'Kunde inte ladda testbokningar');
      }
      state.testBookings = Array.isArray(data.testBookings) ? data.testBookings : [];
      renderTestBookings();
      testBookingsFeedbackEl.textContent = state.testBookings.length + ' testbokningar hämtade.';
    } catch (error) {
      testBookingsFeedbackEl.textContent = 'Fel: ' + error.message;
      testBookingsRowsEl.innerHTML = '';
    }
  }

  async function createBlock(event) {
    event.preventDefault();
    const payload = {
      trailerType: blockTrailerEl.value,
      startDatetime: blockStartEl.value,
      endDatetime: blockEndEl.value,
      reason: blockReasonEl.value.trim()
    };

    blocksFeedbackEl.textContent = 'Skapar blockering...';
    try {
      const response = await adminFetch('/api/admin/blocks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data && data.error ? data.error : 'Kunde inte skapa blockering');
      }
      blockFormEl.reset();
      blocksFeedbackEl.textContent = 'Blockering skapad.';
      await fetchBlocks();
    } catch (error) {
      blocksFeedbackEl.textContent = 'Fel: ' + error.message;
    }
  }

  async function deleteBlock(blockId) {
    blocksFeedbackEl.textContent = 'Tar bort blockering...';
    try {
      const response = await adminFetch('/api/admin/blocks?id=' + encodeURIComponent(blockId), {
        method: 'DELETE'
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data && data.error ? data.error : 'Kunde inte ta bort blockering');
      }
      blocksFeedbackEl.textContent = 'Blockering borttagen.';
      await fetchBlocks();
    } catch (error) {
      blocksFeedbackEl.textContent = 'Fel: ' + error.message;
    }
  }

  async function createTestBooking(event) {
    event.preventDefault();
    const payload = {
      smsTo: testSmsToEl.value.trim(),
      trailerType: testTrailerTypeEl.value,
      date: testDateEl.value || undefined,
      rentalType: testRentalTypeEl.value
    };
    testBookingsFeedbackEl.textContent = 'Skapar testbokning...';
    try {
      const response = await adminFetch('/api/admin/test-bookings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data && data.error ? data.error : 'Kunde inte skapa testbokning');
      }
      latestTestBookingResultEl.textContent =
        'Skapad: ' + (data.bookingReference || ('TEST-' + data.id)) +
        ' | Auto-PAID: ' + formatDateTime(data.autoPaidAt) +
        ' | Raderas: ' + formatDateTime(data.deleteAt);
      testSmsToEl.value = '';
      await fetchTestBookings();
    } catch (error) {
      testBookingsFeedbackEl.textContent = 'Fel: ' + error.message;
    }
  }

  async function runTestBookingsNow() {
    testBookingsFeedbackEl.textContent = 'Kör kontroll...';
    try {
      const response = await adminFetch('/api/admin/test-bookings/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data && data.error ? data.error : 'Kunde inte köra kontroll');
      }
      latestTestBookingResultEl.textContent =
        'Kontroll klar: processedPaid=' + String(data.processedPaid || 0) +
        ', deleted=' + String(data.deleted || 0);
      await fetchTestBookings();
    } catch (error) {
      testBookingsFeedbackEl.textContent = 'Fel: ' + error.message;
    }
  }

  function bindEvents() {
    refreshBookingsBtn.addEventListener('click', fetchBookings);
    refreshBlocksBtn.addEventListener('click', fetchBlocks);
    refreshTestBookingsBtn.addEventListener('click', fetchTestBookings);
    runTestBookingsNowBtn.addEventListener('click', runTestBookingsNow);
    filterDateEl.addEventListener('change', renderBookings);
    filterStatusEl.addEventListener('change', renderBookings);
    filterTrailerEl.addEventListener('change', renderBookings);
    blockFormEl.addEventListener('submit', createBlock);
    testBookingFormEl.addEventListener('submit', createTestBooking);

    blocksRowsEl.addEventListener('click', function (event) {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.classList.contains('delete-block')) return;
      const blockId = target.getAttribute('data-id');
      if (!blockId) return;
      deleteBlock(blockId);
    });
  }

  function initDefaults() {
    const today = new Date().toISOString().slice(0, 10);
    filterDateEl.value = today;
    testDateEl.value = today;
  }

  function startCountdownRefresh() {
    if (state.countdownTimerId !== null) return;
    state.countdownTimerId = window.setInterval(function () {
      if (!state.testBookings.length) return;
      renderTestBookings();
    }, 1000);
  }

  async function init() {
    initDefaults();
    bindEvents();
    startCountdownRefresh();
    await fetchBookings();
    await fetchBlocks();
    await fetchTestBookings();
  }

  init();
})();

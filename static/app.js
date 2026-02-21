/*
 * JavaScript for Dalsjöfors Hyrservice booking wizard.
 */

document.addEventListener('DOMContentLoaded', () => {
  const qs = new URLSearchParams(window.location.search);
  const devMode = qs.get('dev') === '1';

  const state = {
    trailerType: null,
    rentalType: null,
    date: null,
    time: null,
    customerEmail: null,
    receiptRequested: false,
    price: null,
    dayTypeLabel: null,
    available: null,
    blocked: false,
    blockReason: '',
    slotAvailability: [],
    remaining: null,
    remainingByType: { GALLER: null, KAP: null },
    bookingId: null,
    bookingReference: null,
    createdAt: null,
    swishStatus: null,
    pollTimer: null,
    holdPromise: null,
  };

  const progressEl = document.getElementById('progress');
  const progressSteps = Array.from(document.querySelectorAll('#progress-steps li'));
  const steps = {
    1: document.getElementById('step1'),
    2: document.getElementById('step2'),
    3: document.getElementById('step3'),
    4: document.getElementById('step4'),
    5: document.getElementById('step5'),
  };
  let currentStep = 1;

  const cardGaller = document.getElementById('card-galler');
  const cardKap = document.getElementById('card-kap');
  const step1Next = document.getElementById('step1-next');

  const rentalInputs = document.querySelectorAll('input[name="rentalType"]');
  const priceInfo = document.getElementById('price-info');
  const step2Back = document.getElementById('step2-back');
  const step2Next = document.getElementById('step2-next');

  const dateInput = document.getElementById('rental-date');
  const dateError = document.getElementById('date-error');
  const timeContainer = document.getElementById('time-container');
  const timeSelect = document.getElementById('rental-time');
  const timeError = document.getElementById('time-error');
  const receiptRequestedInput = document.getElementById('receipt-requested');
  const receiptEmailWrap = document.getElementById('receiptEmailWrap');
  const customerEmailInput = document.getElementById('customer-email');
  const emailError = document.getElementById('email-error');
  const availabilityInfo = document.getElementById('availability-info');
  const step3Back = document.getElementById('step3-back');
  const step3Next = document.getElementById('step3-next');

  const summaryEl = document.getElementById('summary');
  const step4Back = document.getElementById('step4-back');
  const paymentPanel = document.getElementById('payment-panel');
  const paymentInfo = document.getElementById('payment-info');
  const paymentStatusDot = document.getElementById('payment-status-dot');
  const paymentStatusText = document.getElementById('payment-status-text');
  const qrWrap = document.getElementById('qr-wrap');
  const paymentQr = document.getElementById('payment-qr');
  const openSwishLink = document.getElementById('open-swish-link');
  const checkPaymentStatus = document.getElementById('check-payment-status');
  const retryPayment = document.getElementById('retry-payment');

  const confirmationEl = document.getElementById('confirmation');

  const devPanel = document.getElementById('dev-panel');
  const debugInfo = document.getElementById('debug-info');
  if (devMode) {
    devPanel.hidden = false;
  }

  function formatAvailableCount(remaining) {
    if (typeof remaining !== 'number') {
      return '';
    }
    if (remaining <= 0) {
      return 'Fullbokat';
    }
    return `${remaining} ${remaining === 1 ? 'Tillgänglig' : 'Tillgängliga'}`;
  }

  function showStep(n) {
    currentStep = n;
    progressEl.textContent = `Steg ${n} av 5`;
    progressSteps.forEach(stepNode => {
      const stepNo = Number(stepNode.dataset.step || 0);
      stepNode.classList.remove('is-active', 'is-complete');
      if (stepNo < n) {
        stepNode.classList.add('is-complete');
      } else if (stepNo === n) {
        stepNode.classList.add('is-active');
      }
    });
    Object.keys(steps).forEach(key => {
      const stepNode = steps[key];
      const isTarget = Number(key) === n;
      if (isTarget) {
        stepNode.hidden = false;
        requestAnimationFrame(() => stepNode.classList.add('is-visible'));
      } else {
        stepNode.classList.remove('is-visible');
        stepNode.hidden = true;
      }
    });
  }

  function setInfoState(el, text, stateClass) {
    el.textContent = text;
    el.classList.remove('loading', 'error', 'success', 'warning');
    if (stateClass) {
      el.classList.add(stateClass);
    }
  }

  function setButtonLoading(button, loading) {
    button.disabled = loading;
    button.classList.toggle('is-loading', loading);
  }

  function setFieldError(el, message) {
    if (!el) return;
    const text = (message || '').trim();
    el.textContent = text;
    el.hidden = !text;
  }

  function setPaymentStatusBadge(status) {
    if (!paymentStatusDot || !paymentStatusText) return;
    paymentStatusDot.classList.remove('is-pending', 'is-paid', 'is-failed');
    if (status === 'PAID') {
      paymentStatusDot.classList.add('is-paid');
      paymentStatusText.textContent = 'Betalning registrerad';
      return;
    }
    if (status === 'FAILED') {
      paymentStatusDot.classList.add('is-failed');
      paymentStatusText.textContent = 'Betalning misslyckades';
      return;
    }
    paymentStatusDot.classList.add('is-pending');
    paymentStatusText.textContent = 'Väntar på betalning';
  }

  function stopPaymentPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function isPaid(status) {
    return status === 'PAID';
  }

  function isFailed(status) {
    return status === 'FAILED';
  }

  function readSwishStatus(data, sourceLabel) {
    if (data && typeof data.swishStatus === 'string') {
      return data.swishStatus;
    }
    console.warn(`[payment] Missing swishStatus from ${sourceLabel}. Defaulting to PENDING.`, data);
    return 'PENDING';
  }

  function gotoConfirmation() {
    if (!isPaid(state.swishStatus)) {
      console.warn('[payment] Confirmation blocked because swishStatus is not PAID.', state.swishStatus);
      return;
    }
    renderConfirmation();
    showStep(5);
  }

  function updatePriceInfo() {
    if (!state.rentalType) return;
    if (state.date) {
      setInfoState(priceInfo, 'Hämtar pris …', 'loading');
      fetch(`/api/price?trailerType=${encodeURIComponent(state.trailerType || 'GALLER')}&rentalType=${encodeURIComponent(state.rentalType)}&date=${encodeURIComponent(state.date)}`)
        .then(res => res.json())
        .then(data => {
          if (data && typeof data.price === 'number') {
            state.price = data.price;
            state.dayTypeLabel = data.dayTypeLabel || null;
            const daySuffix = state.dayTypeLabel ? ` (${state.dayTypeLabel})` : '';
            setInfoState(priceInfo, `Pris: ${data.price} kr${daySuffix}`, 'success');
          } else {
            setInfoState(priceInfo, 'Kunde inte hämta pris', 'error');
          }
        })
        .catch(() => {
          setInfoState(priceInfo, 'Kunde inte hämta pris', 'error');
        });
    } else {
      if (state.rentalType === 'FULL_DAY') {
        setInfoState(priceInfo, 'Välj datum för exakt heldagspris', null);
      } else if (state.rentalType === 'TWO_HOURS') {
        setInfoState(priceInfo, 'Pris: 200 kr', null);
      }
    }
  }

  function updateTimeOptions() {
    timeSelect.innerHTML = '';
    const startHour = 8;
    const endHour = 18;
    for (let h = startHour; h <= endHour; h++) {
      for (let m = 0; m < 60; m += 30) {
        const hourStr = h.toString().padStart(2, '0');
        const minuteStr = m.toString().padStart(2, '0');
        const value = `${hourStr}:${minuteStr}`;
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = value;
        timeSelect.appendChild(opt);
      }
    }
  }

  function applyTimeSlotAvailability() {
    const slotMap = new Map((state.slotAvailability || []).map(slot => [slot.time, slot]));
    const options = Array.from(timeSelect.options);
    options.forEach(opt => {
      const slot = slotMap.get(opt.value);
      if (!slot) {
        opt.disabled = false;
        opt.textContent = opt.value;
        return;
      }
      if (slot.available) {
        opt.disabled = false;
        opt.textContent = `${opt.value} (${formatAvailableCount(slot.remaining)})`;
      } else if (slot.blocked && slot.blockReason) {
        opt.disabled = true;
        opt.textContent = `${opt.value} (Blockerad: ${slot.blockReason})`;
      } else {
        opt.disabled = true;
        opt.textContent = `${opt.value} (Fullbokat)`;
      }
    });

    if (!state.time || (slotMap.get(state.time) && !slotMap.get(state.time).available)) {
      const firstAvailable = (state.slotAvailability || []).find(slot => slot.available);
      if (firstAvailable) {
        state.time = firstAvailable.time;
        timeSelect.value = firstAvailable.time;
      }
    }
  }

  function nextAvailableTimeAfterCurrent() {
    if (state.rentalType !== 'TWO_HOURS' || !state.slotAvailability.length) return null;
    const selected = state.time;
    if (!selected) return state.slotAvailability.find(slot => slot.available)?.time || null;
    const currentIndex = state.slotAvailability.findIndex(slot => slot.time === selected);
    if (currentIndex < 0) return state.slotAvailability.find(slot => slot.available)?.time || null;
    for (let i = currentIndex + 1; i < state.slotAvailability.length; i += 1) {
      if (state.slotAvailability[i].available) {
        return state.slotAvailability[i].time;
      }
    }
    return null;
  }

  function updateTimeAvailabilityForDate() {
    if (!state.date || state.rentalType !== 'TWO_HOURS' || !state.trailerType) {
      return Promise.resolve();
    }
    return fetch(
      `/api/availability-slots?trailerType=${encodeURIComponent(state.trailerType)}&rentalType=TWO_HOURS&date=${encodeURIComponent(state.date)}`
    )
      .then(res => res.json().then(data => ({ ok: res.ok, data })))
      .then(({ ok, data }) => {
        if (!ok || !Array.isArray(data.slots)) {
          throw new Error('Kunde inte läsa tider');
        }
        state.slotAvailability = data.slots;
        applyTimeSlotAvailability();
      })
      .catch(() => {
        state.slotAvailability = [];
      });
  }

  function updateAvailabilityInfo() {
    if (state.remaining == null) {
      setInfoState(availabilityInfo, '', null);
      return;
    }
    const availableText = formatAvailableCount(state.remaining);
    if (state.available && availableText !== 'Fullbokat') {
      setInfoState(availabilityInfo, availableText, 'success');
    } else {
      const nextTime = nextAvailableTimeAfterCurrent();
      if (state.blocked && state.blockReason) {
        const suffix = nextTime ? ` Nästa lediga tid: ${nextTime}.` : '';
        setInfoState(availabilityInfo, `Fullbokat (${state.blockReason}).${suffix}`, 'error');
      } else {
        const suffix = nextTime ? ` Nästa lediga tid: ${nextTime}.` : '';
        setInfoState(availabilityInfo, `Fullbokat.${suffix}`, 'error');
      }
    }
    step3Next.disabled = !state.available;
  }

  function updateAvailabilityCounts() {
    if (!state.date || !state.rentalType) return;
    const availabilityPromise = state.rentalType === 'TWO_HOURS' ? updateTimeAvailabilityForDate() : Promise.resolve();
    if (state.trailerType) {
      setInfoState(availabilityInfo, 'Kontrollerar tillgänglighet …', 'loading');
    }
    availabilityPromise.finally(() => {
      const timeParam = state.rentalType === 'TWO_HOURS' && state.time ? `&startTime=${encodeURIComponent(state.time)}` : '';
      ['GALLER', 'KAP'].forEach(type => {
        fetch(`/api/availability?trailerType=${type}&rentalType=${state.rentalType}&date=${state.date}${timeParam}`)
          .then(res => res.json())
          .then(data => {
            state.remainingByType[type] = data.remaining;
            const el = document.getElementById(`availability-${type.toLowerCase()}`);
            if (el) {
              el.textContent = formatAvailableCount(data.remaining);
            }
            if (state.trailerType === type) {
              state.available = data.available;
              state.remaining = data.remaining;
              state.blocked = !!data.blocked;
              state.blockReason = data.blockReason || '';
              updateAvailabilityInfo();
            }
            updateDebugInfo();
          })
          .catch(() => {
            if (state.trailerType === type) {
              setInfoState(availabilityInfo, 'Kunde inte läsa tillgänglighet', 'error');
            }
          });
      });
    });
  }

  function calcEndTime(startTime) {
    if (!startTime) return '';
    const [h, m] = startTime.split(':').map(n => parseInt(n, 10));
    let endH = h + 2;
    if (endH >= 24) endH -= 24;
    return `${endH.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}`;
  }

  function updateSummary() {
    if (!state.trailerType || !state.rentalType || !state.date) return;
    const pieces = [];
    pieces.push(`<p><strong>Släp:</strong> ${state.trailerType === 'GALLER' ? 'Galler-släp' : 'Kåpsläp'}</p>`);
    pieces.push(`<p><strong>Datum:</strong> ${state.date}</p>`);
    if (state.rentalType === 'TWO_HOURS') {
      pieces.push(`<p><strong>Tid:</strong> ${state.time} - ${calcEndTime(state.time)}</p>`);
      pieces.push('<p><strong>Längd:</strong> 2 timmar</p>');
    } else {
      pieces.push('<p><strong>Längd:</strong> Heldag</p>');
    }
    if (state.price != null) {
      const daySuffix = state.dayTypeLabel ? ` (${state.dayTypeLabel})` : '';
      pieces.push(`<p><strong>Pris:</strong> ${state.price} kr${daySuffix}</p>`);
    }
    if (state.receiptRequested) {
      pieces.push('<p><strong>E-postkvitto:</strong> Ja</p>');
      pieces.push(`<p><strong>Mail för kvitto:</strong> ${state.customerEmail ? 'Angivet' : 'Saknas'}</p>`);
    }
    if (state.bookingReference) {
      pieces.push(`<p><strong>Bokningsreferens:</strong> ${state.bookingReference}</p>`);
    }
    summaryEl.innerHTML = pieces.join('');
  }

  function renderConfirmation() {
    const trailerText = state.trailerType === 'GALLER' ? 'Galler-släp' : 'Kåpsläp';
    const nowIso = new Date().toISOString().slice(0, 19).replace('T', ' ');
    const rows = [];
    if (state.bookingReference) {
      rows.push(`<p><strong>Referens:</strong> ${state.bookingReference}</p>`);
    }
    rows.push(`<p><strong>Datum/tid:</strong> ${state.date}${state.rentalType === 'TWO_HOURS' ? ` ${state.time}-${calcEndTime(state.time)}` : ' Heldag'}</p>`);
    rows.push(`<p><strong>Släp:</strong> ${trailerText}</p>`);
    if (state.price != null) {
      rows.push(`<p><strong>Pris:</strong> ${state.price} kr</p>`);
    }
    rows.push(`<p><strong>Betalstatus:</strong> ${state.swishStatus || 'PAID'}</p>`);
    rows.push(`<p><strong>Skapad:</strong> ${state.createdAt || nowIso}</p>`);
    confirmationEl.innerHTML = rows.join('');
  }

  function updateDebugInfo() {
    if (!devMode) return;
    const bits = [];
    if (state.date) {
      const startDT = state.date + (state.rentalType === 'TWO_HOURS' && state.time ? `T${state.time}` : 'T00:00');
      let endDT = '';
      if (state.rentalType === 'TWO_HOURS' && state.time) {
        endDT = state.date + 'T' + calcEndTime(state.time);
      } else if (state.rentalType === 'FULL_DAY') {
        endDT = state.date + 'T23:59';
      }
      bits.push(`<p><strong>start_dt:</strong> ${startDT}</p>`);
      bits.push(`<p><strong>end_dt:</strong> ${endDT}</p>`);
    }
    if (state.bookingId) {
      bits.push(`<p><strong>booking_id:</strong> ${state.bookingId}</p>`);
    }
    if (state.swishStatus) {
      bits.push(`<p><strong>swish_status:</strong> ${state.swishStatus}</p>`);
    }
    debugInfo.innerHTML = bits.join('');
  }

  function createBookingHold() {
    if (state.bookingId) {
      return Promise.resolve(state.bookingId);
    }
    if (state.holdPromise) {
      return state.holdPromise;
    }
    const payload = {
      trailerType: state.trailerType,
      rentalType: state.rentalType,
      date: state.date,
      receiptRequested: !!state.receiptRequested,
      customerEmail: state.receiptRequested ? (state.customerEmail || '') : '',
    };
    if (state.receiptRequested) {
      payload.receiptEmail = state.customerEmail || '';
    }
    if (state.rentalType === 'TWO_HOURS') payload.startTime = state.time;
    state.holdPromise = fetch('/api/hold', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
      .then(res => res.json().then(data => ({ ok: res.ok, data })))
      .then(({ ok, data }) => {
        if (!ok || !data || !data.bookingId) {
          throw new Error(data?.errorInfo?.message || data?.error || 'Kunde inte skapa bokning');
        }
        state.bookingId = data.bookingId;
        state.bookingReference = data.bookingReference || null;
        state.createdAt = data.createdAt || null;
        updateSummary();
        updateDebugInfo();
        return state.bookingId;
      })
      .finally(() => {
        state.holdPromise = null;
      });
    return state.holdPromise;
  }

  function requestSwishPayment() {
    if (!state.bookingId) {
      return Promise.reject(new Error('bookingId saknas'));
    }
    setInfoState(paymentInfo, 'Skapar betalningsförfrågan …', 'loading');
    setPaymentStatusBadge('PENDING');
    paymentPanel.hidden = false;
    retryPayment.hidden = true;

    return fetch(`/api/swish/paymentrequest?bookingId=${encodeURIComponent(state.bookingId)}`, {
      method: 'POST',
    })
      .then(res => res.json().then(data => ({ ok: res.ok, data })))
      .then(({ ok, data }) => {
        if (!ok) {
          throw new Error(data?.errorInfo?.message || data?.error || 'Betalning kunde inte startas');
        }

        const swishStatus = readSwishStatus(data, '/api/swish/paymentrequest');
        state.swishStatus = swishStatus;
        updateDebugInfo();
        if (data.qrUrl) {
          qrWrap.hidden = false;
          paymentQr.src = data.qrUrl;
        }
        if (data.swishAppUrl) {
          openSwishLink.hidden = false;
          openSwishLink.href = data.swishAppUrl;
        } else {
          openSwishLink.hidden = true;
          openSwishLink.removeAttribute('href');
        }

        // Confirmation must only depend on swishStatus (never on generic data.status).
        if (isPaid(swishStatus)) {
          setInfoState(paymentInfo, 'Betalning registrerad.', 'success');
          setPaymentStatusBadge('PAID');
          stopPaymentPolling();
          gotoConfirmation();
          return;
        }

        if (isFailed(swishStatus)) {
          stopPaymentPolling();
          setInfoState(paymentInfo, 'Betalningen misslyckades. Försök igen.', 'error');
          setPaymentStatusBadge('FAILED');
          retryPayment.hidden = false;
          return;
        }

        setPaymentStatusBadge('PENDING');
        setInfoState(paymentInfo, data.idempotent ? 'Väntar på betalning... Återanvänder befintlig förfrågan.' : 'Väntar på betalning...', 'loading');
        startPaymentPolling();
      });
  }

  function checkPaymentStatusOnce() {
    if (!state.bookingId) return;
    setButtonLoading(checkPaymentStatus, true);
    fetch(`/api/payment-status?bookingId=${encodeURIComponent(state.bookingId)}`)
      .then(res => res.json().then(data => ({ ok: res.ok, data })))
      .then(({ ok, data }) => {
        if (!ok) {
          setInfoState(paymentInfo, 'Kunde inte kontrollera betalstatus just nu.', 'warning');
          return;
        }
        const swishStatus = readSwishStatus(data, '/api/payment-status');
        state.swishStatus = swishStatus;
        updateDebugInfo();
        if (isPaid(swishStatus)) {
          setPaymentStatusBadge('PAID');
          setInfoState(paymentInfo, 'Betalning registrerad.', 'success');
          stopPaymentPolling();
          gotoConfirmation();
          return;
        }
        if (isFailed(swishStatus)) {
          setPaymentStatusBadge('FAILED');
          setInfoState(paymentInfo, 'Betalningen misslyckades. Försök igen.', 'error');
          retryPayment.hidden = false;
          stopPaymentPolling();
          return;
        }
        setPaymentStatusBadge('PENDING');
        setInfoState(paymentInfo, 'Fortfarande väntande. Kontrollera igen om några sekunder.', 'loading');
      })
      .catch(() => {
        setInfoState(paymentInfo, 'Kunde inte kontrollera betalstatus just nu.', 'warning');
      })
      .finally(() => {
        setButtonLoading(checkPaymentStatus, false);
      });
  }

  function startPaymentPolling() {
    stopPaymentPolling();
    state.pollTimer = setInterval(() => {
      if (!state.bookingId || currentStep !== 4) return;
      fetch(`/api/payment-status?bookingId=${encodeURIComponent(state.bookingId)}`)
        .then(res => res.json().then(data => ({ ok: res.ok, data })))
        .then(({ ok, data }) => {
          if (!ok) return;
          const swishStatus = readSwishStatus(data, '/api/payment-status');
          state.swishStatus = swishStatus;
          updateDebugInfo();
          if (isPaid(swishStatus)) {
            stopPaymentPolling();
            setPaymentStatusBadge('PAID');
            setInfoState(paymentInfo, 'Betalning registrerad.', 'success');
            gotoConfirmation();
          } else if (isFailed(swishStatus)) {
            stopPaymentPolling();
            setPaymentStatusBadge('FAILED');
            setInfoState(paymentInfo, 'Betalningen misslyckades. Försök igen.', 'error');
            retryPayment.hidden = false;
          } else {
            setPaymentStatusBadge('PENDING');
            setInfoState(paymentInfo, 'Väntar på betalning...', 'loading');
          }
        })
        .catch(() => {
          // Keep polling on transient failures.
        });
    }, 2500);
  }

  function startStep4PaymentFlow() {
    paymentPanel.hidden = false;
    setInfoState(paymentInfo, 'Startar betalningsflöde …', 'loading');
    setPaymentStatusBadge('PENDING');
    qrWrap.hidden = true;
    paymentQr.removeAttribute('src');
    openSwishLink.hidden = true;
    retryPayment.hidden = true;

    createBookingHold()
      .then(() => requestSwishPayment())
      .catch(err => {
        setInfoState(paymentInfo, err.message || 'Kunde inte starta betalning', 'error');
        setPaymentStatusBadge('FAILED');
        retryPayment.hidden = false;
      })
      .finally(() => {
        setButtonLoading(step3Next, false);
      });
  }

  [cardGaller, cardKap].forEach(card => {
    card.addEventListener('click', () => {
      [cardGaller, cardKap].forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
      state.trailerType = card.dataset.type;
      step1Next.disabled = false;
      updateAvailabilityCounts();
      updatePriceInfo();
    });
  });

  step1Next.addEventListener('click', () => {
    showStep(2);
  });

  rentalInputs.forEach(radio => {
    radio.addEventListener('change', () => {
      state.rentalType = radio.value;
      timeContainer.hidden = state.rentalType !== 'TWO_HOURS';
      if (state.rentalType === 'TWO_HOURS') {
        updateTimeOptions();
        state.time = timeSelect.value;
      } else {
        state.time = null;
      }
      updatePriceInfo();
      step2Next.disabled = false;
      if (state.date) {
        updateAvailabilityCounts();
      }
    });
  });

  step2Back.addEventListener('click', () => {
    showStep(1);
  });

  step2Next.addEventListener('click', () => {
    showStep(3);
  });

  dateInput.addEventListener('change', () => {
    setFieldError(dateError, '');
    state.date = dateInput.value;
    updatePriceInfo();
    if (state.rentalType === 'TWO_HOURS') {
      state.time = timeSelect.value;
    } else {
      state.time = null;
    }
    updateAvailabilityCounts();
    step3Next.disabled = true;
  });

  timeSelect.addEventListener('change', () => {
    setFieldError(timeError, '');
    state.time = timeSelect.value;
    updateAvailabilityCounts();
    step3Next.disabled = true;
  });

  function syncReceiptEmailVisibility() {
    const receiptRequested = !!receiptRequestedInput.checked;
    receiptEmailWrap.classList.toggle('hidden', !receiptRequested);
    customerEmailInput.required = receiptRequested;
    if (!receiptRequested) {
      customerEmailInput.value = '';
      customerEmailInput.setCustomValidity('');
      state.customerEmail = null;
      localStorage.removeItem('customerEmail');
    }
  }

  step3Back.addEventListener('click', () => {
    showStep(2);
  });

  step3Next.addEventListener('click', () => {
    setFieldError(dateError, '');
    setFieldError(timeError, '');
    setFieldError(emailError, '');
    if (!state.date) {
      setFieldError(dateError, 'Välj ett datum.');
      return;
    }
    if (state.rentalType === 'TWO_HOURS' && !state.time) {
      setFieldError(timeError, 'Välj en starttid.');
      return;
    }
    const receiptRequested = !!receiptRequestedInput.checked;
    const customerEmail = (customerEmailInput.value || '').trim().toLowerCase();
    if (receiptRequested && (!customerEmail || !customerEmail.includes('@') || customerEmail.length > 254)) {
      setFieldError(emailError, 'Ange en giltig e-postadress för kvitto.');
      return;
    }
    customerEmailInput.setCustomValidity('');
    if (customerEmail && !customerEmailInput.checkValidity()) {
      setFieldError(emailError, 'E-postadressen har fel format.');
      return;
    }
    state.receiptRequested = receiptRequested;
    state.customerEmail = receiptRequested ? (customerEmail || null) : null;
    if (state.customerEmail) {
      localStorage.setItem('customerEmail', state.customerEmail);
    } else {
      localStorage.removeItem('customerEmail');
    }
    if (state.receiptRequested) {
      localStorage.setItem('receiptRequested', '1');
    } else {
      localStorage.removeItem('receiptRequested');
    }
    updateSummary();
    setButtonLoading(step3Next, true);
    showStep(4);
    startStep4PaymentFlow();
  });

  step4Back.addEventListener('click', () => {
    stopPaymentPolling();
    setButtonLoading(step3Next, false);
    showStep(3);
  });

  retryPayment.addEventListener('click', () => {
    requestSwishPayment().catch(err => {
      setInfoState(paymentInfo, err.message || 'Kunde inte skapa betalning', 'error');
      setPaymentStatusBadge('FAILED');
      retryPayment.hidden = false;
    });
  });

  checkPaymentStatus.addEventListener('click', () => {
    checkPaymentStatusOnce();
  });

  const storedCustomerEmail = localStorage.getItem('customerEmail');
  const storedReceiptRequested = localStorage.getItem('receiptRequested');
  if (storedReceiptRequested === '1') {
    state.receiptRequested = true;
    receiptRequestedInput.checked = true;
  }
  if (storedCustomerEmail) {
    state.customerEmail = storedCustomerEmail;
    customerEmailInput.value = storedCustomerEmail;
  }
  syncReceiptEmailVisibility();

  receiptRequestedInput.addEventListener('change', () => {
    state.receiptRequested = !!receiptRequestedInput.checked;
    setFieldError(emailError, '');
    syncReceiptEmailVisibility();
    updateSummary();
  });

  customerEmailInput.addEventListener('input', () => {
    setFieldError(emailError, '');
  });

  showStep(1);
});

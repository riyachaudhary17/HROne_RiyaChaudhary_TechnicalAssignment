const API_BASE = 'http://127.0.0.1:8000';

const outputEl = document.getElementById('output');

function renderOutput(data) {
  outputEl.textContent = JSON.stringify(data, null, 2);
}

function getFormValues(form) {
  const payload = Object.fromEntries(new FormData(form).entries());
  Object.keys(payload).forEach((key) => {
    const value = payload[key];
    if (value === '') {
      delete payload[key];
    }
  });
  return payload;
}

function serializeFormData(form) {
  const data = getFormValues(form);
  Object.keys(data).forEach((key) => {
    const val = data[key];
    if (key.toLowerCase().includes('date') || key === 'month' || key === 'joined_on') {
      return;
    }
    if (val !== undefined && val !== null && val !== '' && !Number.isNaN(Number(val)) && !['emp_code', 'name', 'email', 'department', 'status', 'shift_start', 'shift_end', 'pattern', 'reason', 'regularized_by'].includes(key)) {
      data[key] = Number(val);
    }
  });
  return data;
}

async function apiRequest(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });

  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }

  if (!response.ok) {
    throw new Error(JSON.stringify(data, null, 2));
  }

  return data;
}

async function handleHealth() {
  try {
    const result = await apiRequest('/health');
    renderOutput(result);
  } catch (error) {
    renderOutput({ error: error.message });
  }
}

document.getElementById('healthBtn').addEventListener('click', handleHealth);

document.getElementById('employeeForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const payload = serializeFormData(event.target);
    const result = await apiRequest('/employees', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    renderOutput(result);
    event.target.reset();
  } catch (error) {
    renderOutput({ error: error.message });
  }
});

document.getElementById('listEmployeesBtn').addEventListener('click', async () => {
  try {
    const department = document.getElementById('departmentFilter').value.trim();
    const query = department ? `?department=${encodeURIComponent(department)}` : '';
    const result = await apiRequest(`/employees${query}`);
    renderOutput(result);
  } catch (error) {
    renderOutput({ error: error.message });
  }
});

document.getElementById('punchInForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const payload = serializeFormData(event.target);
    const result = await apiRequest('/attendance/punch-in', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    renderOutput(result);
    event.target.reset();
  } catch (error) {
    renderOutput({ error: error.message });
  }
});

document.getElementById('punchOutForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const payload = serializeFormData(event.target);
    const result = await apiRequest('/attendance/punch-out', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    renderOutput(result);
    event.target.reset();
  } catch (error) {
    renderOutput({ error: error.message });
  }
});

document.getElementById('attendanceForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const form = event.target;
    const params = new URLSearchParams();
    const payload = getFormValues(form);
    Object.entries(payload).forEach(([key, value]) => {
      if (value) params.append(key, value);
    });
    const result = await apiRequest(`/attendance?${params.toString()}`);
    renderOutput(result);
  } catch (error) {
    renderOutput({ error: error.message });
  }
});

document.getElementById('monthlyForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const payload = getFormValues(event.target);
    const params = new URLSearchParams(payload);
    const result = await apiRequest(`/analytics/employees/${encodeURIComponent(payload.emp_code)}/monthly?${params.toString()}`);
    renderOutput(result);
  } catch (error) {
    renderOutput({ error: error.message });
  }
});

document.getElementById('summaryForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const payload = getFormValues(event.target);
    const params = new URLSearchParams();
    if (payload.month) params.append('month', payload.month);
    if (payload.department) params.append('department', payload.department);
    const result = await apiRequest(`/analytics/departments/summary?${params.toString()}`);
    renderOutput(result);
  } catch (error) {
    renderOutput({ error: error.message });
  }
});

document.getElementById('leaderboardForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const payload = getFormValues(event.target);
    const params = new URLSearchParams();
    if (payload.month) params.append('month', payload.month);
    if (payload.limit) params.append('limit', payload.limit);
    if (payload.department) params.append('department', payload.department);
    const result = await apiRequest(`/analytics/leaderboard/late?${params.toString()}`);
    renderOutput(result);
  } catch (error) {
    renderOutput({ error: error.message });
  }
});

document.getElementById('trendForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const payload = getFormValues(event.target);
    const params = new URLSearchParams({
      from: payload.from,
      to: payload.to,
    });
    const result = await apiRequest(`/analytics/departments/${encodeURIComponent(payload.department)}/trend?${params.toString()}`);
    renderOutput(result);
  } catch (error) {
    renderOutput({ error: error.message });
  }
});

renderOutput({ status: 'ready', apiBase: API_BASE });

import TransitMap from "./map/TransitMap";
import { TopBar } from "./components/TopBar";
import { SidePanel } from "./components/SidePanel";
import { StopDisplayPage } from "./components/StopDisplayPage";
import { CityPickerModal } from "./components/CityPickerModal";
import { useVehicleSubscription } from "./mqtt/useVehicleSubscription";
import { useStopArrivalsSubscription } from "./mqtt/useStopsSubscription";
import { useCitiesReferenceData, useCityReferenceData } from "./hooks/useReferenceData";
import { useCityStore } from "./stores/cityStore";
import { DebugGate } from "./components/DebugGate";
import "./app.css";
import "./stop-display.css";

function StopDisplayWrapper() {
  const params = new URLSearchParams(window.location.search);
  const city = params.get("city") ?? "";
  const stop = params.get("stop") ?? "";
  return <StopDisplayPage cityCode={city} stopId={stop} />;
}

function MainApp() {
  useCitiesReferenceData();

  const selectedCityCode = useCityStore((s) => s.selectedCityCode);
  const cities = useCityStore((s) => s.cities);
  const setSelectedCityCode = useCityStore((s) => s.setSelectedCityCode);

  useCityReferenceData(selectedCityCode);
  useVehicleSubscription();
  useStopArrivalsSubscription();

  return (
    <div className="app-shell">
      {!selectedCityCode && cities.length > 0 && (
        <CityPickerModal
          cities={cities}
          onSelect={(code) => setSelectedCityCode(code)}
        />
      )}
      <TopBar />
      <main className="main-layout">
        <TransitMap />
        <SidePanel />
      </main>
    </div>
  );
}

const IS_DEBUG = new URLSearchParams(window.location.search).has("debug");

function App() {
  const params = new URLSearchParams(window.location.search);
  const isStopDisplay = params.get("display") === "stop" && !!params.get("city") && !!params.get("stop");

  if (isStopDisplay) return <StopDisplayWrapper />;
  if (IS_DEBUG) return <DebugGate><MainApp /></DebugGate>;
  return <MainApp />;
}

export default App;

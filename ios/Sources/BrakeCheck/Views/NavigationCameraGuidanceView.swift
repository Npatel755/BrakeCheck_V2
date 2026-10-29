import BrakeCheckCore
import MapKit
import SwiftUI
import UIKit

/// Screen-fixed route cues. These do not claim alignment with camera pixels or lanes.
struct NavigationCameraGuidanceView: View {
    @ObservedObject var navigation: NavigationService
    var useMPH: Bool
    var showsInstruction = true
    @AppStorage("navigation.cameraGuidance") private var enabled = true

    var body: some View {
        if enabled && navigation.isNavigating {
            VStack(alignment: .trailing, spacing: 8) {
                if showsInstruction {
                HStack(spacing: 12) {
                    Image(systemName: navigation.hasArrived ? "checkmark.circle.fill"
                        : NavigationManeuver.symbol(for: navigation.instruction))
                        .font(.brake(size: 30, weight: .bold))
                        .foregroundStyle(navigation.isPaused ? BrakeTheme.warning : BrakeTheme.accent)
                        .frame(width: 40)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(navigation.hasArrived ? "Arrived" : navigation.isPaused ? "Guidance paused" : turnDistance)
                            .font(.brake(.headline)).monospacedDigit()
                        Text(navigation.isPaused ? navigation.status : navigation.instruction)
                            .font(.brake(.subheadline, weight: .semibold)).lineLimit(2)
                        HStack(spacing: 5) {
                            Image(systemName: navigation.hasFreshLocation ? "location.fill" : "location.slash")
                            Text(accuracyText)
                        }
                        .font(.brake(.caption2)).foregroundStyle(BrakeTheme.muted)
                    }
                    Spacer(minLength: 0)
                }
                .padding(12)
                .background(BrakeTheme.cameraSurface, in: RoundedRectangle(cornerRadius: 18))
                }
                if let line = navigation.routePath {
                    NavigationMiniMap(line: line, location: navigation.mapLocation?.coordinate,
                                      current: navigation.hasFreshLocation, estimated: navigation.positionEstimated)
                        .frame(width: 138, height: 105)
                        .clipShape(RoundedRectangle(cornerRadius: 14))
                        .overlay(RoundedRectangle(cornerRadius: 14).stroke(.white.opacity(0.25)))
                        .id(ObjectIdentifier(line))
                }
            }
            .foregroundStyle(BrakeTheme.text)
            .accessibilityElement(children: .combine)
            .allowsHitTesting(false)
        }
    }

    private var accuracyText: String {
        if navigation.positionEstimated {
            return "Estimated position • \(Int((navigation.routeConfidence * 100).rounded()))% confidence • screen-fixed cues"
        }
        guard navigation.hasFreshLocation, let fix = navigation.location,
              fix.horizontalAccuracy.isFinite, fix.horizontalAccuracy >= 0 else { return "Waiting for reliable GPS • screen-fixed cues" }
        return "GPS ±\(Int(fix.horizontalAccuracy.rounded())) m • screen-fixed cues"
    }

    private var turnDistance: String {
        guard let meters = navigation.maneuverDistance, meters.isFinite else { return "Follow your route" }
        if meters < 20 { return "Now" }
        if useMPH {
            return meters >= 160.9344 ? String(format: "In %.1f mi", meters / 1609.344)
                : String(format: "In %.0f ft", meters * 3.28084)
        }
        return meters >= 1000 ? String(format: "In %.1f km", meters / 1000) : String(format: "In %.0f m", meters)
    }
}

private struct NavigationMiniMap: UIViewRepresentable {
    var line: MKPolyline
    var location: CLLocationCoordinate2D?
    var current: Bool
    var estimated: Bool

    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeUIView(context: Context) -> MKMapView {
        let map = MKMapView()
        map.delegate = context.coordinator
        map.isScrollEnabled = false
        map.isZoomEnabled = false
        map.isRotateEnabled = false
        map.isPitchEnabled = false
        map.showsCompass = false
        map.showsUserLocation = false
        map.pointOfInterestFilter = .excludingAll
        map.accessibilityLabel = "Route overview"
        return map
    }
    func updateUIView(_ map: MKMapView, context: Context) {
        let coordinator = context.coordinator
        if coordinator.line !== line {
            if let previous = coordinator.line { map.removeOverlay(previous) }
            coordinator.line = line
            map.addOverlay(line)
            map.setVisibleMapRect(line.boundingMapRect,
                edgePadding: UIEdgeInsets(top: 12, left: 12, bottom: 24, right: 12), animated: false)
        }
        coordinator.current = current
        coordinator.estimated = estimated
        if let location {
            if coordinator.position == nil {
                let annotation = MKPointAnnotation()
                coordinator.position = annotation
                map.addAnnotation(annotation)
            }
            coordinator.position?.coordinate = location
            coordinator.position?.title = estimated ? "Estimated route position" : current ? "GPS position" : "Last GPS position"
            if let position = coordinator.position, let marker = map.view(for: position) as? MKMarkerAnnotationView {
                marker.markerTintColor = estimated ? .systemOrange : current ? .systemBlue : .systemGray
            }
        } else if let position = coordinator.position {
            map.removeAnnotation(position)
            coordinator.position = nil
        }
    }
    @MainActor final class Coordinator: NSObject, MKMapViewDelegate {
        var line: MKPolyline?
        var position: MKPointAnnotation?
        var current = true
        var estimated = false
        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            guard let line = overlay as? MKPolyline else { return MKOverlayRenderer(overlay: overlay) }
            let renderer = MKPolylineRenderer(polyline: line)
            renderer.strokeColor = BrakeTheme.accentUIColor(for: mapView.traitCollection.userInterfaceStyle)
            renderer.lineWidth = 4
            return renderer
        }
        func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
            let marker = MKMarkerAnnotationView(annotation: annotation, reuseIdentifier: "MiniPosition")
            marker.markerTintColor = estimated ? .systemOrange : current ? .systemBlue : .systemGray
            marker.glyphImage = UIImage(systemName: "location.fill")
            marker.displayPriority = .required
            return marker
        }
    }
}

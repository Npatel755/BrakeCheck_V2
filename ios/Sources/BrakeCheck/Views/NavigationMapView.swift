import CoreLocation
import BrakeCheckCore
import MapKit
import SwiftUI
import UIKit

/// Map gestures remain local to the map; attribution stays in its unobstructed footer.
struct NavigationMapView: UIViewRepresentable {
    @Environment(\.colorScheme) private var colorScheme
    var route: MKRoute?
    var location: CLLocation?
    var locationIsCurrent = true
    var followsLocation: Bool
    var recenterTrigger: Int
    var topInset: CGFloat = 40
    var isVisible = true
    var alternatives: [MKRoute] = []
    var stops: [CLLocationCoordinate2D] = []
    var satellite = false
    var showsTraffic = true
    var threeDimensional = true
    var headingUp = true
    var nightMode = "system"
    var routeColor = "accent"
    var routePath: MKPolyline?
    var positionEstimated = false
    var nextTurnDistance: Double?

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> MKMapView {
        let map = NavigationMapSurface()
        map.delegate = context.coordinator
        map.mapType = .standard
        map.pointOfInterestFilter = .excludingAll
        map.isScrollEnabled = true
        map.isZoomEnabled = true
        map.isRotateEnabled = true
        map.isPitchEnabled = true
        map.showsCompass = false
        map.showsScale = true
        map.showsUserLocation = false
        context.coordinator.attach(map)
        let pinch = UIPinchGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.observePinch(_:)))
        pinch.cancelsTouchesInView = false
        pinch.delegate = context.coordinator
        map.addGestureRecognizer(pinch)
        map.mapInteractionPinch = pinch
        let pan = UIPanGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.observePan(_:)))
        pan.maximumNumberOfTouches = 1
        pan.cancelsTouchesInView = false
        pan.delegate = context.coordinator
        map.addGestureRecognizer(pan)
        map.mapInteractionPan = pan
        return map
    }

    static func dismantleUIView(_ map: MKMapView, coordinator: Coordinator) {
        coordinator.displayLink?.invalidate()
        map.delegate = nil
    }

    func updateUIView(_ map: MKMapView, context: Context) {
        let style: UIUserInterfaceStyle = nightMode == "dark" ? .dark : nightMode == "light" ? .light
            : colorScheme == .dark ? .dark : .light
        let styleChanged = map.overrideUserInterfaceStyle != style
        map.overrideUserInterfaceStyle = style
        let coordinator = context.coordinator
        let desiredMapType: MKMapType = satellite ? .hybrid : .standard
        if map.mapType != desiredMapType { map.mapType = desiredMapType }
        map.showsTraffic = showsTraffic
        map.showsBuildings = threeDimensional
        map.showsCompass = true
        let perspectiveChanged = coordinator.threeDimensional != threeDimensional
        coordinator.threeDimensional = threeDimensional
        coordinator.targetPitch = threeDimensional ? 45 : 0
        coordinator.headingUp = headingUp
        let colorChanged = coordinator.routeColor != routeColor
        coordinator.routeColor = routeColor
        coordinator.displayLink?.isPaused = !isVisible
        coordinator.locationIsCurrent = locationIsCurrent
        coordinator.positionEstimated = positionEstimated
        let insetChanged = coordinator.topInset != topInset
        coordinator.topInset = topInset
        let routeChanged = coordinator.route !== route || coordinator.path !== routePath
        let recenter = coordinator.recenterTrigger != recenterTrigger
        let followChanged = coordinator.wasFollowing != followsLocation
        if recenter || (routeChanged && !followsLocation) || (followChanged && followsLocation) {
            coordinator.userPanned = false
        }
        if recenter || (followChanged && followsLocation) { coordinator.manualZoom = false }
        if routeChanged {
            if let previous = coordinator.primaryLine { map.removeOverlay(previous) }
            if let destination = coordinator.destination { map.removeAnnotation(destination) }
            coordinator.destination = nil
            coordinator.route = route
            coordinator.path = routePath
            if let line = coordinator.primaryLine {
                map.addOverlay(line)
                if line.pointCount > 0 {
                    let destination = MKPointAnnotation()
                    destination.coordinate = line.points()[line.pointCount - 1].coordinate
                    destination.title = "Destination"
                    coordinator.destination = destination
                    map.addAnnotation(destination)
                }
            }
        }
        let alternativeLines = alternatives.filter { $0 !== route }.map(\.polyline)
        if !coordinator.alternativeLines.elementsEqual(alternativeLines, by: { $0 === $1 }) {
            map.removeOverlays(coordinator.alternativeLines)
            coordinator.alternativeLines = alternativeLines
            map.addOverlays(alternativeLines, level: .aboveRoads)
            if let line = coordinator.primaryLine { map.removeOverlay(line); map.addOverlay(line, level: .aboveRoads) }
        }
        let stopKey = stops.map { "\($0.latitude),\($0.longitude)" }.joined(separator: ";")
        if stopKey != coordinator.stopKey {
            map.removeAnnotations(coordinator.stopAnnotations)
            coordinator.stopKey = stopKey
            coordinator.stopAnnotations = stops.enumerated().map { index, coordinate in
                let annotation = MKPointAnnotation()
                annotation.coordinate = coordinate
                annotation.title = "Stop \(index + 1)"
                return annotation
            }
            map.addAnnotations(coordinator.stopAnnotations)
        }
        if styleChanged || colorChanged || coordinator.accentName != BrakeTheme.accentName {
            coordinator.accentName = BrakeTheme.accentName
            if let line = coordinator.primaryLine, let renderer = map.renderer(for: line) as? MKPolylineRenderer {
                renderer.strokeColor = coordinator.lineColor(for: style)
                renderer.setNeedsDisplay()
            }
            if let destination = coordinator.destination,
               let marker = map.view(for: destination) as? MKMarkerAnnotationView {
                marker.markerTintColor = coordinator.lineColor(for: style)
            }
        }
        let validLocation = location.flatMap { CLLocationCoordinate2DIsValid($0.coordinate) ? $0 : nil }
        if let location = validLocation {
            if coordinator.userPosition == nil {
                let annotation = MKPointAnnotation()
                annotation.title = "Your location"
                coordinator.userPosition = annotation
                map.addAnnotation(annotation)
            }
            if coordinator.lastLocationTimestamp != location.timestamp {
                let accepted = coordinator.smoother.ingest(NavigationCoordinate(latitude: location.coordinate.latitude,
                    longitude: location.coordinate.longitude), accuracy: location.horizontalAccuracy,
                    speed: location.speed, timestamp: location.timestamp.timeIntervalSince1970,
                    now: Date().timeIntervalSince1970, isCurrent: locationIsCurrent)
                if accepted, locationIsCurrent, location.speed >= 2, location.course >= 0,
                   location.course.isFinite, location.courseAccuracy >= 0, location.courseAccuracy <= 35 {
                    coordinator.targetHeading = location.course
                }
                if let point = coordinator.smoother.coordinate {
                    coordinator.userPosition?.coordinate = CLLocationCoordinate2D(latitude: point.latitude, longitude: point.longitude)
                    coordinator.updateAccuracyCircle(CLLocationCoordinate2D(latitude: point.latitude, longitude: point.longitude))
                }
            }
            coordinator.userPosition?.title = positionEstimated ? "Estimated route position" : locationIsCurrent ? "Your location" : "Last known GPS location"
            if let annotation = coordinator.userPosition, let view = map.view(for: annotation) {
                let icon = coordinator.positionImage()
                if view.image !== icon { view.image = icon }
            }
        } else if let annotation = coordinator.userPosition {
            map.removeAnnotation(annotation)
            coordinator.userPosition = nil
            coordinator.smoother = NavigationMapSmoother()
            if let circle = coordinator.accuracyCircle { map.removeOverlay(circle) }
            coordinator.accuracyCircle = nil
        }
        coordinator.following = followsLocation && validLocation != nil
        if perspectiveChanged, !coordinator.following {
            let camera = map.camera
            camera.pitch = coordinator.targetPitch
            map.setCamera(camera, animated: coordinator.hasCentered)
        }
        if coordinator.following {
            coordinator.needsOverviewFit = false
            if followChanged || recenter || !coordinator.hasCentered {
                coordinator.followDistance = 700
                coordinator.followHeading = map.camera.heading
            }
            if !coordinator.manualZoom, !coordinator.userPanned, !coordinator.userZooming,
               let nextTurnDistance, nextTurnDistance.isFinite, nextTurnDistance >= 0 {
                let measuredSpeed = validLocation?.speed ?? 0
                let speed = measuredSpeed.isFinite ? max(0, measuredSpeed) : 0
                coordinator.followDistance = nextTurnDistance < 80 ? 250
                    : nextTurnDistance < 300 ? 500 : min(1600, max(700, 700 + speed * 25))
            }
            if !coordinator.hasCentered, let location = validLocation {
                map.setCamera(MKMapCamera(lookingAtCenter: location.coordinate, fromDistance: coordinator.followDistance,
                    pitch: coordinator.targetPitch, heading: headingUp ? coordinator.targetHeading : 0), animated: false)
            }
            coordinator.hasCentered = true
        } else if !followsLocation && !coordinator.userPanned && (routeChanged || recenter || insetChanged || !coordinator.hasCentered) {
            if coordinator.primaryLine != nil {
                coordinator.needsOverviewFit = true
            } else if let location = validLocation {
                map.setRegion(MKCoordinateRegion(center: location.coordinate, latitudinalMeters: 1800, longitudinalMeters: 1800), animated: coordinator.hasCentered)
                coordinator.hasCentered = true
            }
        }
        coordinator.lastLocationTimestamp = validLocation?.timestamp
        coordinator.wasFollowing = followsLocation
        coordinator.recenterTrigger = recenterTrigger
    }

    @MainActor final class Coordinator: NSObject, @preconcurrency MKMapViewDelegate, UIGestureRecognizerDelegate {
        var userPanned = false
        var accentName = ""
        private var circleBudget = MapOverlayCoalescer()
        private var circleWasCurrent: Bool?
        private var circleWasEstimated: Bool?
        private var positionImages: [String: UIImage] = [:]
        var route: MKRoute?
        var path: MKPolyline?
        var primaryLine: MKPolyline? { path ?? route?.polyline }
        var alternativeLines: [MKPolyline] = []
        var stopAnnotations: [MKPointAnnotation] = []
        var stopKey = ""
        var routeColor = "accent"
        var targetPitch = 45.0
        var threeDimensional = true
        var headingUp = true
        var destination: MKPointAnnotation?
        var userPosition: MKPointAnnotation?
        var lastLocationTimestamp: Date?
        var recenterTrigger = 0
        var wasFollowing = false
        var hasCentered = false
        var locationIsCurrent = true
        var positionEstimated = false
        var positionColor: UIColor { positionEstimated ? .systemOrange : locationIsCurrent ? .systemBlue : .systemGray }
        var topInset: CGFloat = 40
        var smoother = NavigationMapSmoother()
        weak var map: MKMapView?
        var displayLink: CADisplayLink?
        var accuracyCircle: MKCircle?
        var following = false
        var needsOverviewFit = false
        var followDistance = 700.0
        var manualZoom = false
        var followHeading = 0.0
        var targetHeading = 0.0
        private var lastRender = 0.0
        private var lastCircleUpdate = 0.0
        private var applyingCamera = false
        var userZooming = false
        private var lastBounds = CGSize.zero
        private var boundsChangedAt = 0.0

        @MainActor final class LinkTarget: NSObject {
            weak var owner: Coordinator?
            init(_ owner: Coordinator) { self.owner = owner }
            @objc func step(_ link: CADisplayLink) { owner?.render(at: link.timestamp) }
        }
        func attach(_ map: MKMapView) {
            self.map = map
            let link = CADisplayLink(target: LinkTarget(self), selector: #selector(LinkTarget.step(_:)))
            link.preferredFrameRateRange = CAFrameRateRange(minimum: 15, maximum: 30, preferred: 30)
            link.add(to: .main, forMode: .common)
            displayLink = link
        }
        private func render(at time: Double) {
            guard let map, map.window != nil else { lastRender = 0; return }
            let dt = lastRender == 0 ? 1 / 30.0 : min(0.1, time - lastRender)
            lastRender = time
            if map.bounds.size != lastBounds {
                lastBounds = map.bounds.size
                boundsChangedAt = time
                if !following, !userPanned, primaryLine != nil { needsOverviewFit = true }
            }
            if needsOverviewFit, !following, !userPanned, time - boundsChangedAt > 0.25,
               map.bounds.height > 20, map.bounds.width > 20, let line = primaryLine {
                needsOverviewFit = false
                map.setVisibleMapRect(line.boundingMapRect,
                    edgePadding: UIEdgeInsets(top: min(topInset, map.bounds.height * 0.4), left: 30, bottom: 44, right: 30),
                    animated: hasCentered)
                hasCentered = true
            }
            guard let point = smoother.advance(seconds: dt) else { return }
            let coordinate = CLLocationCoordinate2D(latitude: point.latitude, longitude: point.longitude)
            if let current = userPosition?.coordinate,
               abs(current.latitude - coordinate.latitude) + abs(current.longitude - coordinate.longitude) > 0.0000005 {
                userPosition?.coordinate = coordinate
            }
            if time - lastCircleUpdate > 0.25 {
                updateAccuracyCircle(coordinate)
                lastCircleUpdate = time
            }
            guard following, !userZooming, !userPanned else { return }
            let factor = 1 - exp(-dt / 0.65)
            let current = map.camera
            let longitudeDelta = (coordinate.longitude - current.centerCoordinate.longitude + 540).truncatingRemainder(dividingBy: 360) - 180
            let center = CLLocationCoordinate2D(latitude: current.centerCoordinate.latitude + (coordinate.latitude - current.centerCoordinate.latitude) * factor,
                longitude: (current.centerCoordinate.longitude + longitudeDelta * factor + 540).truncatingRemainder(dividingBy: 360) - 180)
            let turn = ((headingUp ? targetHeading : 0) - followHeading + 540).truncatingRemainder(dividingBy: 360) - 180
            followHeading = (followHeading + turn * factor + 360).truncatingRemainder(dividingBy: 360)
            let distance = current.centerCoordinateDistance + (followDistance - current.centerCoordinateDistance) * factor
            let displacement = MapOverlayCoalescer.distance(
                NavigationCoordinate(latitude: current.centerCoordinate.latitude, longitude: current.centerCoordinate.longitude),
                NavigationCoordinate(latitude: center.latitude, longitude: center.longitude))
            let headingChange = abs((followHeading - current.heading + 540).truncatingRemainder(dividingBy: 360) - 180)
            guard displacement > 0.1 || abs(distance - current.centerCoordinateDistance) > 0.3
                || headingChange > 0.08 || abs(current.pitch - targetPitch) > 0.03 else { return }
            applyingCamera = true
            map.setCamera(MKMapCamera(lookingAtCenter: center, fromDistance: max(180, distance),
                pitch: current.pitch + (targetPitch - current.pitch) * factor, heading: followHeading), animated: false)
            applyingCamera = false
        }

        func updateAccuracyCircle(_ center: CLLocationCoordinate2D) {
            guard let map else { return }
            if accuracyCircle == nil { circleBudget.reset() }
            let replace = circleBudget.accept(center: NavigationCoordinate(latitude: center.latitude, longitude: center.longitude),
                                               radius: max(5, smoother.uncertaintyRadius))
            if !replace {
                if circleWasCurrent != locationIsCurrent || circleWasEstimated != positionEstimated, let circle = accuracyCircle,
                   let renderer = map.renderer(for: circle) as? MKCircleRenderer {
                    let color = positionColor
                    renderer.fillColor = color.withAlphaComponent(0.12)
                    renderer.strokeColor = color.withAlphaComponent(0.35)
                    renderer.setNeedsDisplay()
                }
                circleWasCurrent = locationIsCurrent
                circleWasEstimated = positionEstimated
                return
            }
            if let circle = accuracyCircle { map.removeOverlay(circle) }
            let circle = MKCircle(center: center, radius: max(5, smoother.uncertaintyRadius) + 5)
            accuracyCircle = circle
            circleWasCurrent = locationIsCurrent
            circleWasEstimated = positionEstimated
            map.addOverlay(circle, level: .aboveRoads)
        }
        @objc func observePan(_ gesture: UIPanGestureRecognizer) {
            if gesture.state == .began || gesture.state == .changed {
                userPanned = true
                needsOverviewFit = false
            }
        }
        @objc func observePinch(_ gesture: UIPinchGestureRecognizer) {
            if gesture.state == .began || gesture.state == .changed {
                userZooming = true
                manualZoom = true
            }
            else {
                if let map { followDistance = max(180, min(5000, map.camera.centerCoordinateDistance)) }
                userZooming = false
            }
        }
        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
            shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer) -> Bool {
            if let scroll = otherGestureRecognizer.view as? UIScrollView, scroll.isPagingEnabled { return false }
            return true
        }
        func positionImage() -> UIImage {
            let key = positionEstimated ? "estimated" : locationIsCurrent ? "current" : "stale"
            if let cached = positionImages[key] { return cached }
            let icon = UIGraphicsImageRenderer(size: CGSize(width: 26, height: 26)).image { context in
                let color = positionColor
                color.setFill()
                context.cgContext.fillEllipse(in: CGRect(x: 3, y: 3, width: 20, height: 20))
                UIColor.white.setStroke()
                context.cgContext.setLineWidth(3)
                context.cgContext.strokeEllipse(in: CGRect(x: 3, y: 3, width: 20, height: 20))
            }
            positionImages[key] = icon
            return icon
        }

        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            if let circle = overlay as? MKCircle {
                let renderer = MKCircleRenderer(circle: circle)
                let color = positionColor
                renderer.fillColor = color.withAlphaComponent(0.12)
                renderer.strokeColor = color.withAlphaComponent(0.35)
                renderer.lineWidth = 1
                return renderer
            }
            guard let line = overlay as? MKPolyline else { return MKOverlayRenderer(overlay: overlay) }
            let renderer = MKPolylineRenderer(polyline: line)
            let alternate = alternativeLines.contains { $0 === line }
            renderer.strokeColor = alternate ? UIColor.systemGray.withAlphaComponent(0.7)
                : lineColor(for: mapView.traitCollection.userInterfaceStyle)
            renderer.lineWidth = alternate ? 4 : 7
            renderer.lineCap = .round
            renderer.lineJoin = .round
            return renderer
        }

        func lineColor(for style: UIUserInterfaceStyle) -> UIColor {
            routeColor == "accent" ? BrakeTheme.accentUIColor(for: style)
                : (AccentChoice(rawValue: routeColor) ?? .blue).color(for: style)
        }

        func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
            let isUser = (annotation as AnyObject) === userPosition
            if isUser {
                let view = mapView.dequeueReusableAnnotationView(withIdentifier: "NavigationDot")
                    ?? MKAnnotationView(annotation: annotation, reuseIdentifier: "NavigationDot")
                view.annotation = annotation
                view.image = positionImage()
                view.displayPriority = .required
                view.collisionMode = .circle
                view.accessibilityLabel = positionEstimated ? "Estimated route position" : locationIsCurrent ? "Approximate GPS location" : "Last known GPS location"
                return view
            }
            let identifier = isUser ? "NavigationPosition" : "NavigationDestination"
            let view = (mapView.dequeueReusableAnnotationView(withIdentifier: identifier) as? MKMarkerAnnotationView)
                ?? MKMarkerAnnotationView(annotation: annotation, reuseIdentifier: identifier)
            view.annotation = annotation
            view.markerTintColor = lineColor(for: mapView.traitCollection.userInterfaceStyle)
            view.glyphImage = UIImage(systemName: isUser ? "location.fill" : "flag.fill")
            if let index = stopAnnotations.firstIndex(where: { ($0 as AnyObject) === (annotation as AnyObject) }) {
                view.glyphImage = nil
                view.glyphText = "\(index + 1)"
            } else { view.glyphText = nil }
            view.displayPriority = .required
            view.canShowCallout = false
            return view
        }
    }
}

/// Map touches win over the ancestor page scroll view. Touches outside this
/// map (including the destination panel) remain available to the page pager.
private final class NavigationMapSurface: MKMapView {
    var mapInteractionPan: UIPanGestureRecognizer?
    var mapInteractionPinch: UIPinchGestureRecognizer?
    private weak var protectedPager: UIScrollView?
    override func didMoveToWindow() {
        super.didMoveToWindow()
        protectMapGestures()
    }
    override func didMoveToSuperview() {
        super.didMoveToSuperview()
        protectMapGestures()
    }
    private func protectMapGestures() {
        guard let pan = mapInteractionPan else { return }
        var ancestor = superview
        while let view = ancestor {
            if let pager = view as? UIScrollView, pager.isPagingEnabled {
                if protectedPager !== pager {
                    pager.panGestureRecognizer.require(toFail: pan)
                    if let pinch = mapInteractionPinch { pager.panGestureRecognizer.require(toFail: pinch) }
                    protectedPager = pager
                }
                return
            }
            ancestor = view.superview
        }
    }
}
